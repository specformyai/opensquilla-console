"""Regression checks for lightweight runtime polling (no live services)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import installer


class InstallerPollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "tools" / "opensquilla"
        self.bin = self.runtime / "bin" / "opensquilla"
        self.bin.parent.mkdir(parents=True)
        self.bin.write_text("#!/usr/bin/python3\n")
        self.dist = self.runtime / "lib/python3.12/site-packages/opensquilla-1.2.3.dist-info"
        self.dist.mkdir(parents=True)
        self.metadata = self.dist / "METADATA"
        self.metadata.write_text("Metadata-Version: 2.1\nName: opensquilla\nVersion: 1.2.3\n")
        self.source = {"id": "github", "label": "GitHub Releases"}
        self.release = {"version": "1.2.4", "prerelease": False,
                        "wheel_size": 1024, "url": "https://github.com/opensquilla/opensquilla/releases/tag/v1.2.4"}
        self.manifest = {"schemaVersion": 1, "version": "1.2.4", "tag": "v1.2.4",
                         "baseVersion": "1.2.4", "prerelease": False,
                         "releaseUrl": self.release["url"]}
        self.http_calls = []
        self.cli_calls = []
        self.offline = False
        self.health = {"ok": True, "status": "live"}
        self.gw_url = "http://127.0.0.1:19091"
        self.release_mock = AsyncMock(return_value=[self.release])
        self.fetch_mock = AsyncMock(side_effect=self.fetch)
        self.patches = [
            patch.dict(os.environ, {"UV_TOOL_DIR": str(self.root / "tools"),
                                    "SQUILLA_GATEWAY_HTTP": self.gw_url,
                                    "SQUILLA_CONSOLE_DATA": str(self.root / "data")}),
            patch.object(installer, "OS_BIN", str(self.bin)),
            patch.object(installer, "fetch_json", new=self.fetch_mock),
            patch.object(installer, "resolve_source", new=AsyncMock(return_value=self.source)),
            patch.object(installer, "releases", new=self.release_mock),
            patch.object(installer, "run_capture", side_effect=self.cli),
            patch.object(installer, "_SNAP", {"data": None, "at": 0.0}),
            patch.object(installer, "_SNAP_LOCK", None),
            patch.object(installer, "_UPDATE", {"data": None, "at": 0.0}, create=True),
            patch.object(installer, "_UPDATE_LOCK", None, create=True),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def fetch(self, url, timeout=20):
        self.http_calls.append(url)
        if url == self.gw_url + "/healthz":
            if self.offline:
                raise OSError("offline")
            return self.health
        return self.manifest

    async def cli(self, args, timeout=90):
        self.cli_calls.append(args[1:])
        if args[1:] == ["version", "--json"]:
            return {"rc": 0, "stdout": json.dumps({"version": "9.9.9"}), "stderr": ""}
        if args[1:3] == ["gateway", "status"]:
            return {"rc": 0, "stdout": "Gateway running", "stderr": ""}
        return {"rc": 0, "stdout": json.dumps({"latest": "1.2.4"}), "stderr": ""}

    async def test_reads_target_package_metadata_without_cli(self):
        got = await installer.current_version()
        self.assertEqual(got["version"], "1.2.3")
        self.assertTrue(got["gateway_running"])
        self.assertEqual(self.cli_calls, [])
        self.assertIn(self.gw_url + "/healthz", self.http_calls)

    async def test_resolves_default_uv_launcher_symlink_without_tool_dir(self):
        shim = self.root / "bin" / "opensquilla"
        shim.parent.mkdir()
        shim.symlink_to(self.bin)
        with patch.object(installer, "OS_BIN", str(shim)), patch.dict(os.environ, {"UV_TOOL_DIR": ""}):
            got = await installer.current_version()
        self.assertEqual(got["version"], "1.2.3")
        self.assertEqual(self.cli_calls, [])

    async def test_resolves_command_from_uv_tool_bin_dir_without_cli(self):
        self.bin.chmod(0o755)
        with patch.object(installer, "OS_BIN", "opensquilla"), patch.dict(os.environ, {
            "PATH": str(self.root / "empty-bin"), "UV_TOOL_BIN_DIR": str(self.bin.parent),
            "UV_TOOL_DIR": "",
        }):
            got = await installer.current_version()
        self.assertEqual(got["version"], "1.2.3")
        self.assertEqual(self.cli_calls, [])

    async def test_missing_install_does_not_fall_back_to_cli(self):
        self.metadata.unlink()
        got = await installer.current_version()
        self.assertEqual(got["version"], "")
        self.assertEqual(self.cli_calls, [])

    async def test_offline_gateway_is_not_reported_running(self):
        self.offline = True
        got = await installer.current_version()
        self.assertFalse(got["gateway_running"])
        self.assertEqual(got["version"], "1.2.3")

    async def test_health_requires_true_boolean(self):
        self.health = {"ok": "false"}
        self.assertFalse((await installer.current_version())["gateway_running"])

    async def test_snapshot_uses_one_runtime_query_and_no_cli(self):
        got = await installer.snapshot()
        self.assertEqual(got["current"]["version"], "1.2.3")
        self.assertEqual(got["update"]["latest"], "1.2.4")
        self.assertEqual(self.http_calls.count(self.gw_url + "/healthz"), 1)
        self.assertEqual(self.cli_calls, [])

    async def test_update_metadata_is_cached_across_status_refreshes(self):
        await installer.snapshot()
        installer.invalidate_snapshot()
        self.offline = True
        got = await installer.snapshot()
        self.assertFalse(got["current"]["gateway_running"])
        self.assertFalse(got["update"]["gateway_running"])
        self.assertEqual(self.release_mock.await_count, 1)
        self.assertEqual(self.cli_calls, [])

    async def test_manual_refresh_bypasses_release_cache(self):
        await installer.snapshot()
        await installer.snapshot(force=True)
        self.assertEqual(self.release_mock.await_count, 2)
        self.assertEqual(self.cli_calls, [])

    async def test_cached_release_recalculates_after_install(self):
        await installer.snapshot()
        self.metadata.write_text("Name: opensquilla\nVersion: 1.2.4\n")
        installer.invalidate_snapshot()
        got = await installer.snapshot()
        self.assertEqual(got["update"]["current"], "1.2.4")
        self.assertFalse(got["update"]["update_available"])
        self.assertEqual(self.release_mock.await_count, 1)

    async def test_release_cache_expires(self):
        await installer.snapshot()
        installer._UPDATE["at"] = 0.0
        installer.invalidate_snapshot()
        await installer.snapshot()
        self.assertEqual(self.release_mock.await_count, 2)

    async def test_offline_update_retains_last_good_result_and_cools_down(self):
        await installer.snapshot()
        self.release_mock.side_effect = OSError("offline")
        self.fetch_mock.side_effect = OSError("offline")
        installer._UPDATE["at"] = 0.0
        installer.invalidate_snapshot()
        got = await installer.snapshot()
        self.assertEqual(got["update"]["latest"], "1.2.4")
        self.assertTrue(got["update"]["releases_error"])
        self.assertTrue(got["update"]["channel_error"])
        installer.invalidate_snapshot()
        await installer.snapshot()
        self.assertEqual(self.release_mock.await_count, 2)

    async def test_concurrent_cold_snapshots_share_one_update_query(self):
        values = await asyncio.gather(*(installer.snapshot() for _ in range(6)))
        self.assertEqual(self.release_mock.await_count, 1)
        self.assertTrue(all(v["current"]["version"] == "1.2.3" for v in values))
        self.assertEqual(self.cli_calls, [])

    async def test_unpublished_manifest_does_not_become_install_target(self):
        self.manifest.update(version="1.2.5", tag="v1.2.5", baseVersion="1.2.5",
                             releaseUrl="https://github.com/opensquilla/opensquilla/releases/tag/v1.2.5")
        got = await installer.snapshot()
        self.assertEqual(got["update"]["latest"], "1.2.4")
        self.assertEqual(got["update"]["release_url"], self.release["url"])

    async def test_invalid_manifest_falls_back_to_releases(self):
        self.manifest["tag"] = "v9.9.9"
        got = await installer.snapshot()
        self.assertEqual(got["update"]["latest"], "1.2.4")
        self.assertTrue(got["update"]["channel_error"])

    async def test_explicit_gateway_controls_still_use_cli(self):
        for action in ("start", "stop", "restart", "reload"):
            got = await installer.gateway_action(action)
            self.assertTrue(got["ok"])
        self.assertEqual(self.cli_calls, [["gateway", a] for a in ("start", "stop", "restart", "reload")])


    async def test_source_reprobe_is_visible_without_waiting_for_release_ttl(self):
        await installer.snapshot()
        with patch.object(installer, "resolve_source", new=AsyncMock(return_value={"id": "aliyun"})):
            installer.invalidate_snapshot()
            got = await installer.snapshot()
        self.assertEqual(got["update"]["source"]["id"], "aliyun")

    async def test_loop_refreshes_status_without_forcing_release_checks(self):
        with patch.object(installer, "SNAPSHOT_REFRESH_SECONDS", 0.01):
            installer.start_snapshot_refresh()
            try:
                await asyncio.sleep(0.09)
            finally:
                await installer.stop_snapshot_refresh()
        self.assertGreater(self.http_calls.count(self.gw_url + "/healthz"), 1)
        self.assertEqual(self.release_mock.await_count, 1)
        self.assertEqual(self.cli_calls, [])

    async def test_empty_successful_release_list_clears_install_candidate(self):
        self.release_mock.return_value = []
        got = await installer.snapshot()
        self.assertEqual(got["update"]["latest"], "")
        self.assertFalse(got["update"]["update_available"])

    async def test_conflicting_package_metadata_fails_closed(self):
        other = self.dist.parent / "opensquilla-2.0.0.dist-info"
        other.mkdir()
        (other / "METADATA").write_text("Name: opensquilla\nVersion: 2.0.0\n")
        got = await installer.current_version()
        self.assertEqual(got["version"], "")
        self.assertTrue(got["error"])
        self.assertEqual(self.cli_calls, [])

    async def test_preview_manifest_stays_on_its_release_line(self):
        self.metadata.write_text("Name: opensquilla\nVersion: 1.2.4rc1\n")
        self.manifest.update(version="1.2.4rc2", tag="v1.2.4rc2", prerelease=True,
                             releaseUrl="https://github.com/opensquilla/opensquilla/releases/tag/v1.2.4rc2")
        self.release.update(version="1.2.4rc2", prerelease=True, url=self.manifest["releaseUrl"])
        got = await installer.snapshot()
        self.assertEqual(got["update"]["latest"], "1.2.4rc2")
        self.assertTrue(any(url.endswith("preview/1.2.4.json") for url in self.http_calls))

    async def test_partial_outage_keeps_the_verified_candidate(self):
        first = await installer.check_update()
        self.assertEqual(first["latest"], "1.2.4")
        self.assertEqual([r["version"] for r in first["installable"]], ["1.2.4"])

        # The manifest advertises a newer build while the wheel listing is down.
        # The reused list is the only evidence of what can be downloaded, so the
        # unverified version must not be offered as an install target.
        self.manifest = dict(
            self.manifest, version="1.2.5", tag="v1.2.5", baseVersion="1.2.5",
            releaseUrl=f"https://github.com/{installer.GITHUB_REPO}/releases/tag/v1.2.5")
        self.release_mock.side_effect = OSError("releases unreachable")

        got = await installer.check_update(force=True)
        self.assertTrue(got["releases_error"])
        self.assertEqual(got["latest"], "1.2.4")
        self.assertEqual([r["version"] for r in got["installable"]], ["1.2.4"])
        self.assertEqual(got["latest_wheel_size"], 1024)

    async def install_job(self, *, running=True, stop_rc=0, restart=True, unmanaged=False,
                          offline=True):
        commands = []
        state = "unmanaged" if unmanaged else "unhealthy" if running else "not_started"

        async def capture(args, timeout=90):
            self.assertEqual(args[1:], ["gateway", "status", "--json"])
            return {"rc": 0, "stdout": json.dumps({"ok": True, "state": state}), "stderr": ""}

        async def stream(job, args, timeout=90):
            nonlocal state
            commands.append(args[1:])
            if args[1:] == ["gateway", "stop"]:
                if stop_rc == 0:
                    state = "not_started"
                return stop_rc
            if args[1:3] == ["tool", "install"]:
                self.metadata.write_text("Name: opensquilla\nVersion: 1.2.4\n")
            return 0

        pre = {"ok": True, "checks": [], "source": self.source}
        self.offline = offline
        with patch.object(installer, "runner", installer.JobRunner()), \
                patch.object(installer, "preflight", new=AsyncMock(return_value=pre)), \
                patch.object(installer, "run_capture", side_effect=capture), \
                patch.object(installer, "_stream", side_effect=stream):
            job = await installer.install_version("1.2.4", restart_gateway=restart)
            task = installer.runner._task
            assert task is not None
            await task
        return job, commands

    async def test_install_stops_unhealthy_process_before_replacing_environment(self):
        job, commands = await self.install_job()
        self.assertEqual(job.state, "done")
        self.assertEqual(commands[0], ["gateway", "stop"])
        self.assertEqual(commands[1][:2], ["tool", "install"])
        self.assertEqual(commands[2], ["gateway", "start"])

    async def test_failed_stop_prevents_environment_replacement(self):
        job, commands = await self.install_job(stop_rc=1)
        self.assertEqual(job.state, "failed")
        self.assertFalse(any(c[:2] == ["tool", "install"] for c in commands))

    async def test_no_restart_cannot_replace_a_live_environment(self):
        job, commands = await self.install_job(restart=False)
        self.assertEqual(job.state, "failed")
        self.assertEqual(commands, [])

    async def test_no_restart_checks_lifecycle_for_uv_tool_bin_command(self):
        self.bin.chmod(0o755)
        with patch.object(installer, "OS_BIN", "opensquilla"), patch.dict(os.environ, {
            "PATH": str(self.root / "empty-bin"), "UV_TOOL_BIN_DIR": str(self.bin.parent),
            "UV_TOOL_DIR": "",
        }):
            job, commands = await self.install_job(restart=False)
        self.assertEqual(commands, [])
        self.assertEqual(job.state, "failed")
        self.assertIn("Stop the gateway", job.error or "")

    async def test_missing_launcher_with_healthy_gateway_blocks_install(self):
        self.bin.unlink()
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.metadata.unlink(missing_ok=True)
                job, commands = await self.install_job(restart=restart, offline=False)
                self.assertEqual(commands, [])
                self.assertEqual(job.state, "failed")
                self.assertIn("lifecycle command", job.error or "")

    async def test_missing_launcher_with_metadata_blocks_install(self):
        self.bin.unlink()
        job, commands = await self.install_job(restart=False)
        self.assertEqual(commands, [])
        self.assertEqual(job.state, "failed")
        self.assertIn("lifecycle command", job.error or "")

    async def test_missing_runtime_and_offline_gateway_allows_fresh_install(self):
        self.bin.unlink()
        self.metadata.unlink()
        job, commands = await self.install_job(running=False, restart=False)
        self.assertEqual(job.state, "done")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:2], ["tool", "install"])

    async def test_unmanaged_gateway_is_not_stopped_or_overwritten(self):
        job, commands = await self.install_job(unmanaged=True)
        self.assertEqual(job.state, "failed")
        self.assertEqual(commands, [])

    async def test_stopped_install_with_no_restart_keeps_gateway_offline(self):
        job, commands = await self.install_job(running=False, restart=False)
        self.assertEqual(job.state, "done")
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][:2], ["tool", "install"])


if __name__ == "__main__":
    unittest.main()