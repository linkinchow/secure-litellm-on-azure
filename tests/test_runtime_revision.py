import fnmatch
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from scripts.customer_migration import ROOT
from scripts.runtime_revision import ANNOTATION, RUNTIME_INPUTS, build_runtime_revision, receipt_current, require_clean, runtime_inputs, runtime_revision, selected, signature_binding


SOURCES = {
    "LiteLLM/runtime/Dockerfile": "FROM base@sha256:" + "0" * 64 + "\nCOPY LiteLLM/runtime /opt/llmgw/LiteLLM/runtime\n",
    "LiteLLM/runtime/Dockerfile.dockerignore": "**\n!LiteLLM/runtime/\n",
    "LiteLLM/runtime/application.py": "print('gateway')\n",
    "LiteLLM/runtime/security-requirements.txt": "anyio==4.14.2 --hash=sha256:" + "1" * 64 + "\n",
    "scripts/source_supply_chain.py": "RUNTIME_CONTRACT = {}\n",
    "scripts/schema_runtime.py": "MODE = 'inspect'\n",
    "scripts/collector_image.py": "COLLECTOR_DIGEST = 'sha256:" + "2" * 64 + "'\n",
    "scripts/stage9_release.py": "RELEASE = 1\n",
    "auth-proxy/Dockerfile": "FROM node@sha256:" + "3" * 64 + "\nCOPY *.mjs ./\n",
    "auth-proxy/.dockerignore": "node_modules\ntest\n",
    "auth-proxy/package.json": "{}\n",
    "auth-proxy/package-lock.json": "{}\n",
    "auth-proxy/native-ui-routes.json": "[]\n",
    "auth-proxy/server.mjs": "export {};\n",
    "auth-proxy/test/server.test.mjs": "export {};\n",
    "auth-proxy/README_ZH.md": "代理\n",
    "docs/guide-zh.md": "指南\n",
    ".gitignore": "__pycache__/\n*.pyc\n.env\n",
}


class Repository:
    def __init__(self, root):
        self.root = Path(root)
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.write(SOURCES)

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.root), "-c", "init.defaultBranch=main", "-c", "user.name=runtime-revision", "-c", "user.email=runtime-revision@example.invalid", "-c", "commit.gpgsign=false", *arguments], check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()

    def write(self, files):
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, files=None):
        if files:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", "change")
        return self.git("rev-parse", "HEAD")

    def revisions(self, revision):
        return {runtime: runtime_revision(runtime, revision, self.root) for runtime in RUNTIME_INPUTS}


def payload(image, runtime="azure", environment="test", **annotations):
    return [{"critical": {"image": {"docker-manifest-digest": image.rsplit("@", 1)[1]}}, "optional": {"llmgw.runtime": runtime, "llmgw.environment": environment, **annotations}}]


class RuntimeRevisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.directory.cleanup)
        self.repository = Repository(self.directory.name)
        self.base = self.repository.commit()

    def test_docs_and_stage_code_commits_keep_every_runtime_revision(self):
        before = self.repository.revisions(self.base)
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in before.values()))
        self.assertEqual(len(set(before.values())), len(before))
        later = self.repository.commit({"docs/guide-zh.md": "新的指南\n", "scripts/stage9_release.py": "RELEASE = 2\n", "auth-proxy/README_ZH.md": "更新\n", "auth-proxy/test/server.test.mjs": "export const changed = 1;\n", "auth-proxy/entra/api-application.json": "{}\n"})
        self.assertNotEqual(later, self.base)
        self.assertEqual(self.repository.revisions(later), before)

    def test_image_and_contract_inputs_change_only_their_runtime(self):
        before = self.repository.revisions(self.base)
        cases = (
            ({"LiteLLM/runtime/Dockerfile": SOURCES["LiteLLM/runtime/Dockerfile"].replace("0" * 64, "4" * 64)}, {"azure"}),
            ({"LiteLLM/runtime/application.py": "print('changed')\n"}, {"azure"}),
            ({"LiteLLM/runtime/codex_passthrough.py": "HOOK = 1\n"}, {"azure"}),
            ({"LiteLLM/runtime/security-requirements.txt": "anyio==4.14.3\n"}, {"azure"}),
            ({"scripts/source_supply_chain.py": "RUNTIME_CONTRACT = {'uid': 10001}\n"}, {"azure"}),
            ({"scripts/schema_runtime.py": "MODE = 'execute'\n"}, {"azure"}),
            ({"auth-proxy/server.mjs": "export const changed = 1;\n"}, {"auth-proxy"}),
            ({"auth-proxy/guardrail.mjs": "export {};\n"}, {"auth-proxy"}),
            ({"auth-proxy/package-lock.json": "{\"lockfileVersion\": 3}\n"}, {"auth-proxy"}),
            ({"scripts/collector_image.py": "COLLECTOR_DIGEST = 'sha256:" + "5" * 64 + "'\n"}, {"collector"}),
        )
        previous = before
        for files, changed in cases:
            with self.subTest(files=sorted(files)):
                current = self.repository.revisions(self.repository.commit(files))
                self.assertEqual({runtime for runtime in current if current[runtime] != previous[runtime]}, changed)
                previous = current
        self.repository.git("update-index", "--chmod=+x", "LiteLLM/runtime/application.py")
        executable = self.repository.revisions(self.repository.commit())
        self.assertEqual({runtime for runtime in executable if executable[runtime] != previous[runtime]}, {"azure"})

    def test_missing_inputs_and_partial_revisions_are_refused(self):
        with self.assertRaisesRegex(ValueError, "full source revision"):
            runtime_revision("azure", self.base[:12], self.repository.root)
        with self.assertRaisesRegex(ValueError, "Unknown runtime"):
            runtime_revision("l3-audit", self.base, self.repository.root)
        with self.assertRaisesRegex(ValueError, "Git tree"):
            runtime_revision("azure", "f" * 40, self.repository.root)
        self.repository.git("rm", "-q", "scripts/collector_image.py")
        removed = self.repository.commit()
        with self.assertRaisesRegex(ValueError, "lacks required runtime inputs"):
            runtime_revision("collector", removed, self.repository.root)

    def test_build_requires_a_clean_checkout_of_the_reviewed_revision(self):
        root = self.repository.root
        expected = runtime_revision("azure", self.base, root)
        self.assertEqual(build_runtime_revision("azure", self.base, root), expected)
        with self.assertRaisesRegex(ValueError, "checkout of the reviewed revision"):
            require_clean("azure", "f" * 40, root)
        # Files docker build never copies, or regenerated bytecode, do not block the build.
        self.repository.write({"docs/guide-zh.md": "草稿\n", "LiteLLM/runtime/__pycache__/application.cpython-313.pyc": "bytecode", "auth-proxy/test/extra.test.mjs": "export {};\n", "auth-proxy/node_modules/x/index.js": ""})
        self.assertEqual(build_runtime_revision("azure", self.base, root), expected)
        build_runtime_revision("auth-proxy", self.base, root)
        for runtime, files in (
            ("azure", {"LiteLLM/runtime/application.py": "print('uncommitted')\n"}),
            ("azure", {"LiteLLM/runtime/untracked.py": "x = 1\n"}),
            ("azure", {"LiteLLM/runtime/.env": "SECRET=ignored-but-copied\n"}),
            ("azure", {"scripts/schema_runtime.py": "MODE = 'local'\n"}),
            ("auth-proxy", {"auth-proxy/local.mjs": "export {};\n"}),
            ("collector", {"scripts/collector_image.py": "COLLECTOR_DIGEST = ''\n"}),
        ):
            with self.subTest(files=sorted(files)):
                original = {name: SOURCES[name] for name in files if name in SOURCES}
                self.repository.write(files)
                with self.assertRaisesRegex(ValueError, "Commit or remove local changes"):
                    build_runtime_revision(runtime, self.base, root)
                for name in files:
                    if name in original:
                        self.repository.write({name: original[name]})
                    else:
                        (root / name).unlink()
        self.repository.git("rm", "-q", "--cached", "LiteLLM/runtime/application.py")
        with self.assertRaisesRegex(ValueError, "Commit or remove local changes"):
            build_runtime_revision("azure", self.base, root)

    def test_signature_binding_prefers_runtime_revision_and_keeps_the_legacy_commit_path(self):
        image = "customerregistry.azurecr.io/litellm-azure@sha256:" + "a" * 64
        commit, current = "b" * 40, "c" * 64
        self.assertEqual(signature_binding(payload(image, **{"llmgw.revision": "d" * 40, ANNOTATION: current}), image, "azure", "test", commit, current), "runtimeRevision")
        self.assertEqual(signature_binding(payload(image, **{"llmgw.revision": commit}), image, "azure", "test", commit, current), "revision")
        rejected = (
            payload(image, **{"llmgw.revision": "d" * 40}),
            payload(image, **{"llmgw.revision": commit, ANNOTATION: "e" * 64}),
            payload(image, **{ANNOTATION: "e" * 64}),
            payload(image, runtime="auth-proxy", **{ANNOTATION: current}),
            payload(image, environment="prod", **{ANNOTATION: current}),
            payload(image.replace("a" * 64, "f" * 64), **{ANNOTATION: current}),
            [{"optional": {ANNOTATION: current}}],
            [],
        )
        for signatures in rejected:
            with self.subTest(signatures=signatures):
                self.assertIsNone(signature_binding(signatures, image, "azure", "test", commit, current))
        mixed = payload(image, **{"llmgw.revision": "d" * 40}) + payload(image, **{ANNOTATION: current})
        self.assertEqual(signature_binding(mixed, image, "azure", "test", commit, current), "runtimeRevision")
        with self.assertRaisesRegex(ValueError, "unexpected payload"):
            signature_binding({}, image, "azure", "test", commit, current)

    def test_receipts_bind_runtime_revision_with_a_legacy_commit_fallback(self):
        commit, current = "b" * 40, "c" * 64
        self.assertTrue(receipt_current({"revision": "d" * 40, "runtimeRevision": current}, commit, current))
        self.assertFalse(receipt_current({"revision": commit, "runtimeRevision": "e" * 64}, commit, current))
        self.assertTrue(receipt_current({"revision": commit}, commit, current))
        self.assertFalse(receipt_current({"revision": "d" * 40}, commit, current))
        self.assertFalse(receipt_current({}, commit, current))


class RepositoryRuntimeInputTests(unittest.TestCase):
    def test_every_copied_build_input_is_part_of_its_runtime_revision(self):
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True, capture_output=True, text=True).stdout.split("\0")
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        for runtime, dockerfile, context in (("azure", "LiteLLM/runtime/Dockerfile", ""), ("auth-proxy", "auth-proxy/Dockerfile", "auth-proxy/")):
            sources = []
            for line in (ROOT / dockerfile).read_text().splitlines():
                if line.startswith("COPY ") and "--from=" not in line:
                    sources.extend(word for word in line.split()[1:-1] if not word.startswith("--"))
            self.assertTrue(sources)
            for source in sources:
                with self.subTest(runtime=runtime, source=source):
                    relative = [path[len(context):] for path in tracked if path.startswith(context)]
                    if any(character in source for character in "*?["):
                        matches = [context + path for path in relative if "/" not in path and fnmatch.fnmatchcase(path, source)]
                    else:
                        matches = [context + path for path in relative if path == source or path.startswith(source + "/")]
                    self.assertTrue(matches)
                    self.assertTrue(all(selected(runtime, path) for path in matches))
            ignore = dockerfile + ".dockerignore" if runtime == "azure" else context + ".dockerignore"
            self.assertTrue({dockerfile, ignore} <= {path for path, _mode, _blob in runtime_inputs(runtime, head)})
        self.assertIn("LiteLLM/runtime/**/__pycache__", (ROOT / "LiteLLM/runtime/Dockerfile.dockerignore").read_text().splitlines())

    def test_collector_digest_has_a_single_reviewed_source(self):
        from scripts import collector_image, observability
        self.assertIs(observability.COLLECTOR_DIGEST, collector_image.COLLECTOR_DIGEST)
        self.assertEqual(RUNTIME_INPUTS["collector"], {"files": ("scripts/collector_image.py",)})
        self.assertNotIn(collector_image.COLLECTOR_DIGEST.split(":")[1], (ROOT / "scripts/observability.py").read_text())

    def test_promotion_workflow_signs_and_verifies_the_runtime_revision(self):
        workflow = (ROOT / ".github/workflows/promote-litellm-image.yml").read_text()
        self.assertIn('python -m scripts.runtime_revision --runtime "$runtime" --revision "$GITHUB_SHA" --require-clean', workflow)
        for runtime in ("azure", "auth-proxy"):
            self.assertIn(f'cosign sign --yes -a llmgw.runtime={runtime} -a "llmgw.revision=$GITHUB_SHA" -a "llmgw.runtimeRevision=$RUNTIME_REVISION"', workflow)
            self.assertIn(f'annotations+=(-a llmgw.runtime={runtime} -a "llmgw.revision=$GITHUB_SHA" -a "llmgw.runtimeRevision=$RUNTIME_REVISION"', workflow)
        self.assertIn('"runtimeRevision": os.environ["RUNTIME_REVISION"] or None', workflow)
        self.assertLess(workflow.index("Compute the runtime revision"), workflow.index("Build and push the reviewed Azure application runtime"))


if __name__ == "__main__":
    unittest.main()
