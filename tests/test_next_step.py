#!/usr/bin/env python3
"""Regression and integration test suite for the Antigravity Next Step HUD plugin (Route A)."""

from __future__ import annotations

import fcntl
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import next_step_core as core  # noqa: E402
import next_step_floating  # noqa: E402
import next_step_hook as hook  # noqa: E402
import next_step_hud_server as hud_server  # noqa: E402
import next_step_mcp_server as mcp_server  # noqa: E402


class NextStepPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._prev_no_auto = os.environ.get("NEXT_STEP_NO_AUTO_FLOATING_HUD")
        os.environ["NEXT_STEP_NO_AUTO_FLOATING_HUD"] = "1"
        self._tmp_data = tempfile.TemporaryDirectory()
        self._tmp_ws = tempfile.TemporaryDirectory()
        self._tmp_art = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp_data.name).resolve()
        self.ws_dir = Path(self._tmp_ws.name).resolve()
        self.art_dir = Path(self._tmp_art.name).resolve()
        core.set_data_dir(self.data_dir)
        core.ensure_data_dirs()

    def tearDown(self) -> None:
        if self._prev_no_auto is None:
            os.environ.pop("NEXT_STEP_NO_AUTO_FLOATING_HUD", None)
        else:
            os.environ["NEXT_STEP_NO_AUTO_FLOATING_HUD"] = self._prev_no_auto
        self._tmp_art.cleanup()
        self._tmp_ws.cleanup()
        self._tmp_data.cleanup()

    def _base_payload(self, cid: str = "a7bea7d3-0c0d-4be2-ad3b-34fe546c1a23", **extra: object) -> dict:
        payload: dict[str, object] = {
            "conversationId": cid,
            "workspacePaths": [str(self.ws_dir)],
            "artifactDirectoryPath": str(self.art_dir),
            "transcriptPath": str(self.art_dir / "transcript.jsonl"),
            "modelName": "Orchestrator (Neon)",
            "lastUserInput": "<USER_REQUEST>把报告 PDF 的上下页边距改成 15mm，先改脚本别跑</USER_REQUEST>",
        }
        payload.update(extra)
        return payload

    def test_case_a_agent_declared_next_step(self) -> None:
        """Case A: Agent publishes an explicit next step with a linked plan file and suggested prompt."""
        cid = "a7bea7d3-0c0d-4be2-ad3b-34fe546c1a23"
        plan_file = self.art_dir / "implementation_plan.md"
        plan_file.write_text("# Plan v1\n- [ ] Update margins to 15mm\n", encoding="utf-8")

        # 1. PreInvocation (turn starts)
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1))
        st_working = core.load_session(cid)
        self.assertEqual(st_working["lifecycle"], "WORKING")
        self.assertFalse(st_working["waitingOnUser"])

        # 2. PreToolUse intercepts next_step_publish and injects overwrite fields
        pub_args = {
            "next_action": "确认 generate_pdf.py 页边距改动，随后触发脚本重跑并核对 PDF 页数",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": "跑一下 reports/generate_pdf.py 并核对生成的 PDF 页数与目录",
            "detail": "仅修改了上下页边距参数为 15mm，未改动表格样式",
            "plan_path": "implementation_plan.md",
        }
        pre_out = hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=14,
                toolCall={"id": "tc_14", "name": "mcp_next-step_next_step_publish", "args": pub_args},
            )
        )
        self.assertEqual(pre_out["decision"], "allow")
        self.assertEqual(pre_out["overwrite"]["_sys_conversation_id"], cid)

        # 3. MCP server executes next_step_publish (with _ensure_hud=False to avoid spawning external daemon in unit test)
        mcp_args = dict(pub_args, **pre_out["overwrite"], _ensure_hud=False)
        mcp_res = mcp_server._handle_tool_call("next_step_publish", mcp_args)
        self.assertFalse(mcp_res["isError"])
        self.assertIn("确认 generate_pdf.py 页边距改动", mcp_res["content"][0]["text"])

        # 4. PostToolUse completes
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=14,
                toolCall={"id": "tc_14", "name": "mcp_next-step_next_step_publish", "args": pub_args},
            )
        )

        # 5. Same-turn continuation PreInvocation (invocationNum=2) before final text reply MUST preserve pendingDeclaration
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=2))
        st_cont = core.load_session(cid)
        self.assertIsNotNone(st_cont["pendingDeclaration"])

        # 6. Stop hook fires at turn completion
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))
        final_state = core.evaluate_staleness(core.load_session(cid))

        self.assertEqual(final_state["lifecycle"], "IDLE")
        self.assertEqual(final_state["sourceType"], "AGENT_DECLARED")
        self.assertEqual(final_state["confidence"], "HIGH")
        self.assertTrue(final_state["waitingOnUser"])
        self.assertEqual(final_state["actor"], "user")
        self.assertEqual(
            final_state["nextAction"],
            "确认 generate_pdf.py 页边距改动，随后触发脚本重跑并核对 PDF 页数",
        )
        self.assertEqual(
            final_state["suggestedPrompt"],
            "跑一下 reports/generate_pdf.py 并核对生成的 PDF 页数与目录",
        )
        self.assertFalse(final_state["stale"])
        self.assertIsNotNone(final_state["planArtifact"])
        self.assertFalse(final_state["planArtifact"]["stale"])

    def test_case_b_waiting_on_user_decision_and_plan_review(self) -> None:
        """Case B: Deterministic approval states (ask_question, ask_permission, RequestFeedback artifact)."""
        cid = "a7bea7d3-0c0d-4be2-ad3b-34fe546c1a23"

        # Subcase B1: ask_question
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="跑一下"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=18,
                toolCall={
                    "id": "tc_18",
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "请确认要执行的脚本入口",
                                "options": [
                                    "(Recommended) reports/generate_pdf.py",
                                    "docs/build_docx.js",
                                    "backend/server.py",
                                ],
                            }
                        ]
                    },
                },
            )
        )
        # Even if Stop fires while waiting for user answer, APPROVAL_REQUIRED is preserved
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))
        st_q = core.load_session(cid)
        self.assertEqual(st_q["lifecycle"], "WAITING_USER_DECISION")
        self.assertEqual(st_q["sourceType"], "APPROVAL_REQUIRED")
        self.assertEqual(st_q["confidence"], "DETERMINISTIC")
        self.assertTrue(st_q["waitingOnUser"])
        self.assertEqual(st_q["actor"], "user")
        self.assertIn("请确认要执行的脚本入口", st_q["nextAction"])
        self.assertEqual(len(st_q["options"]), 3)

        # Once user answers, PostToolUse clears the blocking question state
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=18,
                toolCall={"id": "tc_18", "name": "ask_question", "args": {}},
            )
        )
        st_after_q = core.load_session(cid)
        self.assertEqual(st_after_q["lifecycle"], "WORKING")
        self.assertFalse(st_after_q["waitingOnUser"])

        # Subcase B2: RequestFeedback plan artifact write
        plan_path = self.art_dir / "implementation_plan.md"
        plan_path.write_text("# Implementation Plan\nStep 1\n", encoding="utf-8")
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=20,
                toolCall={
                    "id": "tc_20",
                    "name": "write_file",
                    "args": {
                        "Path": str(plan_path),
                        "ArtifactMetadata": {
                            "Summary": "报告 PDF 页边距调整方案",
                            "UserFacing": True,
                            "RequestFeedback": True,
                        },
                    },
                },
            )
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="model_stop", fullyIdle=True))
        st_plan = core.load_session(cid)
        self.assertEqual(st_plan["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertEqual(st_plan["sourceType"], "APPROVAL_REQUIRED")
        self.assertTrue(st_plan["waitingOnUser"])
        self.assertIn("implementation_plan.md", st_plan["nextAction"])
        self.assertEqual(st_plan["detail"], "报告 PDF 页边距调整方案")

    def test_case_c_no_credible_next_step(self) -> None:
        """Case C: Turn finishes normally with neither pending approval nor agent declaration -> NONE."""
        cid = "a7bea7d3-0c0d-4be2-ad3b-34fe546c1a23"
        hook.handle_pre_invocation(
            self._base_payload(
                cid,
                invocationNum=1,
                lastUserInput="跑一下 reports/generate_pdf.py 并核对生成的 PDF 页数与目录",
            )
        )
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=25,
                toolCall={
                    "id": "tc_25",
                    "name": "run_command",
                    "args": {"CommandLine": "python3 generate_pdf.py", "toolSummary": "Generate PDF"},
                },
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=25,
                toolCall={"id": "tc_25", "name": "run_command", "args": {}},
            )
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        st = core.load_session(cid)
        self.assertEqual(st["lifecycle"], "IDLE")
        self.assertEqual(st["sourceType"], "NONE")
        self.assertEqual(st["confidence"], "DETERMINISTIC")
        self.assertFalse(st["waitingOnUser"])
        self.assertEqual(st["actor"], "none")
        self.assertIsNone(st["nextAction"])
        md = core.format_session_markdown(st)
        self.assertIn("当前无已声明的下一步", md)

    def test_plan_rewrite_marks_declaration_stale(self) -> None:
        """Rewriting a linked plan artifact without publishing a new step marks the old instruction stale."""
        cid = "conv-plan-rewrite-001"
        plan_file = self.art_dir / "implementation_plan.md"
        plan_file.write_text("# Plan v1\nOriginal step\n", encoding="utf-8")

        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1))
        pub_args = {
            "next_action": "按 Plan v1 执行第一步",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": "开始执行 Plan v1",
            "plan_path": str(plan_file),
        }
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                toolCall={"id": "tc_3", "name": "next_step_publish", "args": pub_args},
            )
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        st_v1 = core.evaluate_staleness(core.load_session(cid))
        self.assertFalse(st_v1["stale"])
        self.assertFalse(st_v1["planArtifact"]["stale"])

        # Now modify the plan file on disk (simulating a plan rewrite)
        plan_file.write_text("# Plan v2 (rewritten)\nCompletely different steps\n", encoding="utf-8")

        # Dynamic read-time staleness detection immediately catches the SHA-256 mismatch
        st_eval = core.evaluate_staleness(core.load_session(cid))
        self.assertTrue(st_eval["stale"])
        self.assertTrue(st_eval["planArtifact"]["stale"])
        self.assertIn("计划文件已改写", st_eval["staleReason"])

        # PostToolUse on edit_file also persists the staleness flag
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=4,
                toolCall={"id": "tc_4", "name": "edit_file", "args": {"Path": str(plan_file)}},
            )
        )
        st_persisted = core.load_session(cid)
        self.assertTrue(st_persisted["stale"])
        self.assertTrue(st_persisted["planArtifact"]["stale"])

    def test_session_isolation_and_fork_clean_start(self) -> None:
        """Switching between Session A, Session B, and a forked Session C never leaks instructions."""
        cid_a = "session-aaaa-1111"
        cid_b = "session-bbbb-2222"
        cid_fork = "session-fork-3333"

        # Session A publishes a next step
        hook.handle_pre_invocation(self._base_payload(cid_a, invocationNum=1, lastUserInput="任务 A"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_a,
                stepIdx=2,
                toolCall={
                    "id": "tc_a",
                    "name": "call_mcp_tool",
                    "args": {
                        "ServerName": "next-step_next_step",
                        "ToolName": "next_step_publish",
                        "Arguments": {
                            "next_action": "会话 A 的专属下一步",
                            "actor": "user",
                            "waiting_on_user": True,
                        },
                    },
                },
            )
        )
        hook.handle_stop(self._base_payload(cid_a, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        # Session B is waiting on ask_permission
        hook.handle_pre_invocation(self._base_payload(cid_b, invocationNum=1, lastUserInput="任务 B"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_b,
                stepIdx=5,
                toolCall={
                    "id": "tc_b",
                    "name": "ask_permission",
                    "args": {"Action": "command", "Target": "git status", "Reason": "Check repo status"},
                },
            )
        )

        # Forked Session C starts fresh
        hook.handle_session_start(self._base_payload(cid_fork, lastUserInput="任务 A (Fork)"))

        st_a = core.load_session(cid_a)
        st_b = core.load_session(cid_b)
        st_fork = core.load_session(cid_fork)

        self.assertEqual(st_a["nextAction"], "会话 A 的专属下一步")
        self.assertEqual(st_a["sourceType"], "AGENT_DECLARED")

        self.assertEqual(st_b["lifecycle"], "WAITING_USER_APPROVAL")
        self.assertIn("command(git status)", st_b["nextAction"])

        self.assertEqual(st_fork["sourceType"], "NONE")
        self.assertIsNone(st_fork["nextAction"])

    def test_cancel_error_and_background_stop_states(self) -> None:
        """Stop hook properly invalidates pending declarations on USER_CANCELED or ERROR, and tracks fullyIdle=False."""
        cid = "session-cancel-err-001"

        # Stage a declaration, then user cancels the turn
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                toolCall={
                    "id": "tc_2",
                    "name": "next_step_publish",
                    "args": {"next_action": "会被取消作废的声明", "actor": "user", "waiting_on_user": True},
                },
            )
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="USER_CANCELED", fullyIdle=True))
        st_cancel = core.load_session(cid)
        self.assertEqual(st_cancel["lifecycle"], "CANCELED")
        self.assertIsNone(st_cancel["pendingDeclaration"])
        self.assertIn("手动中止", st_cancel["nextAction"])

        # Next turn ends with an error
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="重试一次"))
        hook.handle_stop(
            self._base_payload(cid, terminationReason="ERROR", error="Quota exceeded", fullyIdle=True)
        )
        st_err = core.load_session(cid)
        self.assertEqual(st_err["lifecycle"], "ERROR")
        self.assertEqual(st_err["sourceType"], "APPROVAL_REQUIRED")
        self.assertTrue(st_err["waitingOnUser"])
        self.assertIn("Quota exceeded", st_err["nextAction"])

        # Next turn pauses with background tasks still running (fullyIdle=False)
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="后台跑任务"))
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=False))
        st_bg = core.load_session(cid)
        self.assertEqual(st_bg["lifecycle"], "BACKGROUND_RUNNING")
        self.assertEqual(st_bg["actor"], "agent_background")
        self.assertFalse(st_bg["waitingOnUser"])

    def test_mcp_dispatch_and_in_process_hud_server(self) -> None:
        """Verify MCP JSON-RPC initialize/tools/call and HTTP HUD server endpoints (/healthz, /api/sessions, /)."""
        init_resp = mcp_server._dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        self.assertIsNotNone(init_resp)
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "next-step")
        self.assertIn("resources", init_resp["result"]["capabilities"])

        list_resp = mcp_server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tool_names = {t["name"] for t in list_resp["result"]["tools"]}
        self.assertEqual(
            tool_names,
            {"next_step_publish", "next_step_get", "next_step_side_panel", "next_step_panel_state"},
        )

        cid = "hud-test-session-12345678"
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1))
        call_resp = mcp_server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "next_step_publish",
                    "arguments": {
                        "conversation_id": cid,
                        "next_action": "在 HUD 中验证会话卡片渲染",
                        "actor": "user",
                        "waiting_on_user": True,
                        "suggested_prompt": "验证通过，继续下一步",
                        "_ensure_hud": False,
                    },
                },
            }
        )
        self.assertFalse(call_resp["result"]["isError"])

        get_resp = mcp_server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "next_step_get",
                    "arguments": {"conversation_id": cid, "ensure_hud": False},
                },
            }
        )
        self.assertFalse(get_resp["result"]["isError"])
        self.assertIn("在 HUD 中验证会话卡片渲染", get_resp["result"]["content"][0]["text"])

        httpd, thread, url = hud_server.start_in_process_server(host="127.0.0.1", port=0)
        try:
            with urllib.request.urlopen(url + "healthz", timeout=2.0) as r:
                health = json.loads(r.read().decode("utf-8"))
                self.assertTrue(health["ok"])

            with urllib.request.urlopen(url + "api/sessions", timeout=2.0) as r:
                api_data = json.loads(r.read().decode("utf-8"))
                self.assertTrue(api_data["ok"])
                self.assertEqual(len(api_data["sessions"]), 1)
                self.assertEqual(api_data["sessions"][0]["nextAction"], "在 HUD 中验证会话卡片渲染")

            with urllib.request.urlopen(url, timeout=2.0) as r:
                html_body = r.read().decode("utf-8")
                self.assertIn("Next Step HUD", html_body)
                self.assertIn("在 HUD 中验证会话卡片渲染", html_body)
                self.assertIn("复制指令", html_body)
        finally:
            hud_server.stop_in_process_server(httpd, thread)

    def test_statusline_companion_script(self) -> None:
        """Verify CLI statusline.sh script formats waiting and declared states properly."""
        cid = "cli-session-99887766"
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1))
        core.stage_declaration(
            {
                "next_action": "运行 pytest 验证改动",
                "actor": "user",
                "waiting_on_user": True,
            },
            cid=cid,
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        script_path = SCRIPTS_DIR / "statusline.sh"
        env = dict(os.environ, PLUGIN_DATA=str(self.data_dir))
        proc = subprocess.run(
            ["/bin/sh", str(script_path)],
            input=json.dumps({"conversation_id": cid, "agent_state": "idle", "tool_confirmation_pending": False}),
            text=True,
            capture_output=True,
            env=env,
            check=True,
        )
        self.assertIn("运行 pytest 验证改动", proc.stdout)
        self.assertIn("由谁做: 用户", proc.stdout)

    def test_call_mcp_tool_publish_does_not_leak_across_sessions(self) -> None:
        """[P1 Regression] Wrapping next_step_publish in call_mcp_tool or unattributed MCP calls must never leak to another session's Stop."""
        cid_a = "session-leak-check-A"
        cid_b = "session-leak-check-B"

        hook.handle_pre_invocation(self._base_payload(cid_a, invocationNum=1, lastUserInput="会话 A 任务"))
        wrapped_tc = {
            "id": "tc_wrap_a",
            "name": "call_mcp_tool",
            "args": {
                "ServerName": "next-step_next_step",
                "ToolName": "next_step_publish",
                "Arguments": {
                    "next_action": "仅属于会话 A 的下一步指令",
                    "actor": "user",
                    "waiting_on_user": True,
                },
            },
        }
        pre_out = hook.handle_pre_tool_use(self._base_payload(cid_a, stepIdx=3, toolCall=wrapped_tc))
        self.assertEqual(pre_out["decision"], "allow")
        self.assertEqual(pre_out["overwrite"]["Arguments"]["_sys_conversation_id"], cid_a)

        # Simulate MCP receiving either raw unattributed arguments or overwritten arguments
        mcp_server._handle_tool_call(
            "next_step_publish",
            {
                "next_action": "仅属于会话 A 的下一步指令",
                "actor": "user",
                "waiting_on_user": True,
                "_ensure_hud": False,
            },
        )
        hook.handle_post_tool_use(self._base_payload(cid_a, stepIdx=3, toolCall=wrapped_tc))
        hook.handle_stop(self._base_payload(cid_a, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        # Session B starts and stops without publishing anything
        hook.handle_pre_invocation(self._base_payload(cid_b, invocationNum=1, lastUserInput="会话 B 任务"))
        hook.handle_stop(self._base_payload(cid_b, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        st_a = core.load_session(cid_a)
        st_b = core.load_session(cid_b)
        self.assertEqual(st_a["sourceType"], "AGENT_DECLARED")
        self.assertEqual(st_a["nextAction"], "仅属于会话 A 的下一步指令")
        self.assertEqual(st_b["sourceType"], "NONE")
        self.assertIsNone(st_b["nextAction"])
        self.assertFalse(st_b["waitingOnUser"])

    def test_waiting_plan_review_survives_same_turn_continuation_and_tools(self) -> None:
        """[P1 Regression] RequestFeedback=True plan review state survives same-turn PreInvocation(invocationNum=2), helper tool calls, and Stop."""
        cid = "session-plan-review-persist"
        plan_path = self.art_dir / "implementation_plan.md"
        plan_path.write_text("# Plan v1\n", encoding="utf-8")

        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="先出方案"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                lastUserInput="先出方案",
                toolCall={"id": "tc_w", "name": "write_file", "args": {"Path": str(plan_path)}},
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                lastUserInput="先出方案",
                toolCall={
                    "id": "tc_w",
                    "name": "write_file",
                    "args": {
                        "Path": str(plan_path),
                        "ArtifactMetadata": {
                            "Summary": "待审阅的核心架构方案",
                            "UserFacing": True,
                            "RequestFeedback": True,
                        },
                    },
                },
            )
        )

        # Even if an intermediate tool event omits lastUserInput, same-turn PreInvocation(2) preserves WAITING_PLAN_REVIEW
        hook.handle_pre_tool_use(
            {
                "conversationId": cid,
                "workspacePaths": [str(self.ws_dir)],
                "artifactDirectoryPath": str(self.art_dir),
                "stepIdx": 2,
                "toolCall": {"id": "tc_noop", "name": "list_dir", "args": {"DirectoryPath": str(self.ws_dir)}},
            }
        )
        hook.handle_post_tool_use(
            {
                "conversationId": cid,
                "workspacePaths": [str(self.ws_dir)],
                "artifactDirectoryPath": str(self.art_dir),
                "stepIdx": 2,
                "toolCall": {"id": "tc_noop", "name": "list_dir", "args": {"DirectoryPath": str(self.ws_dir)}},
            }
        )

        # Same-turn continuation (invocationNum=2) and an ordinary read/edit tool before Stop
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=2, lastUserInput="先出方案"))
        st_mid = core.load_session(cid)
        self.assertEqual(st_mid["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertTrue(st_mid["waitingOnUser"])

        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                lastUserInput="先出方案",
                toolCall={"id": "tc_r", "name": "view_file", "args": {"AbsolutePath": str(plan_path)}},
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                lastUserInput="先出方案",
                toolCall={"id": "tc_r", "name": "view_file", "args": {"AbsolutePath": str(plan_path)}},
            )
        )
        hook.handle_stop(
            self._base_payload(cid, lastUserInput="先出方案", terminationReason="NO_TOOL_CALL", fullyIdle=True)
        )

        st_stop = core.evaluate_staleness(core.load_session(cid))
        self.assertEqual(st_stop["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertEqual(st_stop["sourceType"], "APPROVAL_REQUIRED")
        self.assertTrue(st_stop["waitingOnUser"])
        self.assertFalse(st_stop["stale"])
        self.assertIn("implementation_plan.md", st_stop["nextAction"])

        # Once the user starts a brand-new turn (Proceed), WAITING_PLAN_REVIEW is cleared
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="Proceed"))
        st_next_turn = core.load_session(cid)
        self.assertEqual(st_next_turn["lifecycle"], "WORKING")
        self.assertFalse(st_next_turn["waitingOnUser"])
        self.assertIsNone(st_next_turn["pendingPlanReview"])

    def test_new_declaration_without_plan_path_clears_old_stale_plan(self) -> None:
        """[P2 Regression] A new turn or replacement declaration without plan_path must not inherit an old rewritten plan's staleness."""
        cid = "session-clear-old-plan"
        plan_file = self.art_dir / "implementation_plan.md"
        plan_file.write_text("# Plan v1\n", encoding="utf-8")

        # Turn 1 binds plan_file
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="第一轮带计划"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                toolCall={
                    "id": "tc_p1",
                    "name": "next_step_publish",
                    "args": {
                        "next_action": "执行第一轮计划",
                        "actor": "user",
                        "waiting_on_user": True,
                        "plan_path": str(plan_file),
                    },
                },
            )
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        # Rewrite plan_file on disk so Turn 1 becomes stale
        plan_file.write_text("# Plan v2 modified\n", encoding="utf-8")
        self.assertTrue(core.evaluate_staleness(core.load_session(cid))["stale"])

        # Turn 2 publishes an action WITHOUT plan_path
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="第二轮无关任务"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=5,
                toolCall={
                    "id": "tc_p2",
                    "name": "next_step_publish",
                    "args": {
                        "next_action": "运行独立脚本验证结果",
                        "actor": "user",
                        "waiting_on_user": True,
                        "suggested_prompt": "python3 check.py",
                    },
                },
            )
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))

        st_turn2 = core.evaluate_staleness(core.load_session(cid))
        self.assertEqual(st_turn2["sourceType"], "AGENT_DECLARED")
        self.assertEqual(st_turn2["nextAction"], "运行独立脚本验证结果")
        self.assertIsNone(st_turn2["planArtifact"])
        self.assertFalse(st_turn2["stale"])
        self.assertIsNone(st_turn2["staleReason"])

        # Same-turn replacement via MCP next_step_publish without plan_path over an existing stale state also clears planArtifact
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="第三轮同轮替换"))
        mcp_server._handle_tool_call(
            "next_step_publish",
            {
                "conversation_id": cid,
                "next_action": "第三轮绑定旧计划",
                "actor": "user",
                "waiting_on_user": True,
                "plan_path": str(plan_file),
                "_ensure_hud": False,
            },
        )
        plan_file.write_text("# Plan v3 modified\n", encoding="utf-8")
        self.assertTrue(core.evaluate_staleness(core.load_session(cid))["stale"])

        mcp_server._handle_tool_call(
            "next_step_publish",
            {
                "conversation_id": cid,
                "next_action": "改为无需计划的独立检查",
                "actor": "user",
                "waiting_on_user": True,
                "_ensure_hud": False,
            },
        )
        hook.handle_stop(self._base_payload(cid, lastUserInput="第三轮同轮替换", terminationReason="NO_TOOL_CALL", fullyIdle=True))
        st_turn3 = core.evaluate_staleness(core.load_session(cid))
        self.assertEqual(st_turn3["nextAction"], "改为无需计划的独立检查")
        self.assertIsNone(st_turn3["planArtifact"])
        self.assertFalse(st_turn3["stale"])

    def test_waiting_plan_review_survives_background_pause_stop_and_resume(self) -> None:
        """[Round 2 P1 Regression] Stop(fullyIdle=False) followed by same-turn PreInvocation(2) must not drop WAITING_PLAN_REVIEW."""
        cid = "session-bg-pause-resume"
        plan_path = self.art_dir / "implementation_plan.md"
        plan_path.write_text("# Plan v1\n", encoding="utf-8")
        transcript_path = self.art_dir / "transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "先出方案"}) + "\n",
            encoding="utf-8",
        )

        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="先出方案"))
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                lastUserInput="先出方案",
                toolCall={
                    "id": "tc_w",
                    "name": "write_file",
                    "args": {
                        "Path": str(plan_path),
                        "ArtifactMetadata": {"Summary": "背景暂停测试方案", "UserFacing": True, "RequestFeedback": True},
                    },
                },
            )
        )
        # Background pause Stop(fullyIdle=False)
        hook.handle_stop(self._base_payload(cid, lastUserInput="先出方案", terminationReason="NO_TOOL_CALL", fullyIdle=False))
        st_paused = core.load_session(cid)
        self.assertEqual(st_paused["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertTrue(st_paused["turnActive"])

        # Same-turn continuation after background wakeup (same USER_EXPLICIT step, invocationNum=2)
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=2, lastUserInput="先出方案"))
        st_resumed = core.load_session(cid)
        self.assertEqual(st_resumed["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertTrue(st_resumed["waitingOnUser"])

        hook.handle_stop(self._base_payload(cid, lastUserInput="先出方案", terminationReason="NO_TOOL_CALL", fullyIdle=True))
        st_final = core.load_session(cid)
        self.assertEqual(st_final["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertEqual(st_final["sourceType"], "APPROVAL_REQUIRED")
        self.assertTrue(st_final["waitingOnUser"])

        # A real new user input in transcript clears the previous turn's plan review
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"step_index": 5, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "Proceed"}) + "\n"
            )
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="Proceed"))
        st_new = core.load_session(cid)
        self.assertEqual(st_new["lifecycle"], "WORKING")
        self.assertIsNone(st_new["pendingPlanReview"])

    def test_same_turn_edit_or_delete_plan_marks_review_stale_without_clobbering_sha(self) -> None:
        """[Round 2 P2 Regression] Ordinary edit_file or delete_file on a pending RequestFeedback plan keeps original SHA-256 baseline and marks stale=True."""
        cid = "session-plan-stale-baseline"
        plan_path = self.art_dir / "implementation_plan.md"
        plan_path.write_text("# Plan Baseline 1\n", encoding="utf-8")
        h1, _ = core.compute_file_sha256(plan_path)

        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="拟定方案"))
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                lastUserInput="拟定方案",
                toolCall={
                    "id": "tc_w1",
                    "name": "write_file",
                    "args": {
                        "Path": str(plan_path),
                        "ArtifactMetadata": {"Summary": "基线方案 v1", "UserFacing": True, "RequestFeedback": True},
                    },
                },
            )
        )
        self.assertFalse(core.load_session(cid)["stale"])
        self.assertEqual(core.load_session(cid)["planArtifact"]["sha256"], h1)

        # Ordinary edit_file without RequestFeedback=True modifies the plan file
        plan_path.write_text("# Plan Edited Silently\n", encoding="utf-8")
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                lastUserInput="拟定方案",
                toolCall={"id": "tc_e1", "name": "edit_file", "args": {"Path": str(plan_path)}},
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                lastUserInput="拟定方案",
                toolCall={"id": "tc_e1", "name": "edit_file", "args": {"Path": str(plan_path)}},
            )
        )
        hook.handle_stop(self._base_payload(cid, lastUserInput="拟定方案", terminationReason="NO_TOOL_CALL", fullyIdle=True))

        st_edited = core.evaluate_staleness(core.load_session(cid))
        self.assertEqual(st_edited["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertEqual(st_edited["planArtifact"]["sha256"], h1)
        self.assertTrue(st_edited["stale"])
        self.assertTrue(st_edited["planArtifact"]["stale"])

        # Explicit re-request of feedback establishes a new clean baseline H2
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="重新提交审阅"))
        plan_path.write_text("# Plan Baseline 2\n", encoding="utf-8")
        h2, _ = core.compute_file_sha256(plan_path)
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=5,
                lastUserInput="重新提交审阅",
                toolCall={
                    "id": "tc_w2",
                    "name": "write_file",
                    "args": {
                        "Path": str(plan_path),
                        "ArtifactMetadata": {"Summary": "基线方案 v2", "UserFacing": True, "RequestFeedback": True},
                    },
                },
            )
        )
        st_re = core.evaluate_staleness(core.load_session(cid))
        self.assertEqual(st_re["planArtifact"]["sha256"], h2)
        self.assertFalse(st_re["stale"])

        # Deleting the plan file preserves H2 as baseline and marks stale=True
        plan_path.unlink()
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=6,
                lastUserInput="重新提交审阅",
                toolCall={"id": "tc_d1", "name": "delete_file", "args": {"Path": str(plan_path)}},
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=6,
                lastUserInput="重新提交审阅",
                toolCall={"id": "tc_d1", "name": "delete_file", "args": {"Path": str(plan_path)}},
            )
        )
        hook.handle_stop(self._base_payload(cid, lastUserInput="重新提交审阅", terminationReason="NO_TOOL_CALL", fullyIdle=True))

        st_deleted = core.evaluate_staleness(core.load_session(cid))
        self.assertEqual(st_deleted["lifecycle"], "WAITING_PLAN_REVIEW")
        self.assertEqual(st_deleted["planArtifact"]["sha256"], h2)
        self.assertTrue(st_deleted["stale"])
        self.assertTrue(st_deleted["planArtifact"]["stale"])

    def test_compact_hud_route_and_webkit_clipboard_bridge(self) -> None:
        """Verify /?mode=compact renders compact floating mode, option copy chips, WebKit clipboard bridge, and /healthz version."""
        cid = "compact-hud-test-001"
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="选一个入口"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                lastUserInput="选一个入口",
                toolCall={
                    "id": "tc_q",
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "请选择执行目标",
                                "options": ["方案 A", "方案 B"],
                            }
                        ]
                    },
                },
            )
        )

        httpd, thread, url = hud_server.start_in_process_server(host="127.0.0.1", port=0)
        try:
            with urllib.request.urlopen(url + "healthz", timeout=2.0) as r:
                health = json.loads(r.read().decode("utf-8"))
                self.assertEqual(health.get("version"), hud_server.HUD_SERVER_VERSION)

            with urllib.request.urlopen(url + "?mode=compact", timeout=2.0) as r:
                compact_html = r.read().decode("utf-8")
                self.assertIn('class="mode-compact"', compact_html)
                self.assertIn("window.webkit.messageHandlers.copyText", compact_html)
                self.assertIn("copyOption", compact_html)
                self.assertIn("copyPrompt", compact_html)
                self.assertIn("方案 A", compact_html)
        finally:
            hud_server.stop_in_process_server(httpd, thread)

    def test_floating_hud_swift_compiles_cleanly(self) -> None:
        """Verify floating_hud.swift compiles cleanly via swiftc and caches binary by SHA-256."""
        if next_step_floating.find_swiftc() is None:
            self.skipTest("swiftc not available in environment")

        t0 = time.time()
        bin_path = next_step_floating.compile_floating_hud(self.data_dir)
        self.assertTrue(bin_path.is_file())
        self.assertTrue(os.access(bin_path, os.X_OK))
        mtime_1 = bin_path.stat().st_mtime_ns

        # Second call hits SHA-256 cache without recompiling
        bin_path_2 = next_step_floating.compile_floating_hud(self.data_dir)
        self.assertEqual(bin_path, bin_path_2)
        self.assertEqual(bin_path_2.stat().st_mtime_ns, mtime_1)
        self.assertGreater(time.time() - t0, 0.0)

        swift_src = next_step_floating.SWIFT_SOURCE_PATH.read_text(encoding="utf-8")
        self.assertIn("NSWindow.contentRect(", swift_src)
        self.assertIn("hudPanel.setFrame(initialFrame", swift_src)
        self.assertIn("writeDismissFile()", swift_src)

        usage_proc = subprocess.run([str(bin_path)], text=True, capture_output=True, timeout=5.0)
        self.assertEqual(usage_proc.returncode, 2)
        self.assertIn("--dismiss-file", usage_proc.stderr)

    def test_floating_hud_pid_identity_and_stale_cleanup(self) -> None:
        """[Round 1 P1 Regression] Unrelated live PID in floating_hud.json must never receive SIGUSR1/SIGTERM and is cleaned up."""
        _, _, state_path, _ = next_step_floating._floating_paths(self.data_dir)
        unrelated_pid = os.getpid()
        state_path.write_text(
            json.dumps({"pid": unrelated_pid, "url": "http://127.0.0.1:9999/?mode=full"}),
            encoding="utf-8",
        )
        kill_calls: list[tuple[int, int]] = []
        orig_kill = os.kill

        def spy_kill(pid: int, sig: int) -> None:
            kill_calls.append((pid, sig))
            return orig_kill(pid, sig)

        with mock.patch("os.kill", side_effect=spy_kill):
            res = next_step_floating.is_floating_hud_running(self.data_dir)
        self.assertIsNone(res)
        self.assertFalse(state_path.exists())
        self.assertTrue(all(sig == 0 for _, sig in kill_calls))

        # Spoofed command lines containing substring in arguments must still be rejected
        fake_bin = str(self.data_dir / "bin" / "next-step-floating-hud")
        for spoof_comm, spoof_args in (
            ("/usr/bin/tail", "/usr/bin/tail -f /tmp/next-step-floating-hud.log"),
            ("/usr/bin/vim", f"/usr/bin/vim {fake_bin}"),
            ("/usr/bin/python3", f"/usr/bin/python3 -c pass {fake_bin}"),
        ):
            with (
                mock.patch.object(next_step_floating, "_is_pid_alive", return_value=True),
                mock.patch.object(
                    next_step_floating,
                    "_query_ps_field",
                    side_effect=lambda _pid, field, c=spoof_comm, a=spoof_args: c if field == "comm" else a,
                ),
            ):
                self.assertFalse(next_step_floating._verify_floating_hud_process(99999, expected_executable=fake_bin))

        # Spoofed HUD server command lines must also be rejected
        hud_script = str(Path(hud_server.__file__).resolve())
        for spoof_comm, spoof_args in (
            ("/usr/bin/tail", f"/usr/bin/tail -f {hud_script}"),
            ("/usr/local/bin/python3", f"/usr/local/bin/python3 -m http.server {hud_script}"),
            ("/usr/local/bin/python3", f"/usr/local/bin/python3 /tmp/other.py --ref {hud_script}"),
        ):
            def fake_ps_run(cmd, **kwargs):
                field = cmd[-1].rstrip("=")
                out = spoof_comm if field == "comm" else spoof_args
                return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

            with mock.patch.object(hud_server.subprocess, "run", side_effect=fake_ps_run):
                self.assertFalse(hud_server._verify_hud_server_process(99999))

        # Genuine HUD server command line with matching script & data-dir must pass
        def real_ps_run(cmd, **kwargs):
            field = cmd[-1].rstrip("=")
            out = (
                "/usr/local/bin/python3"
                if field == "comm"
                else f"/usr/local/bin/python3 -u {hud_script} --host 127.0.0.1 --port 18765 --data-dir {self.data_dir}"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")

        with mock.patch.object(hud_server.subprocess, "run", side_effect=real_ps_run):
            self.assertTrue(hud_server._verify_hud_server_process(99999))

    def test_floating_hud_concurrent_launch_lock_single_instance(self) -> None:
        """[Round 1 P2 Regression] Concurrent launch_floating_hud calls serialize via flock and only spawn once."""
        bin_path = self.data_dir / "bin" / "next-step-floating-hud"
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
        _, _, state_path, _ = next_step_floating._floating_paths(self.data_dir)

        spawn_count = 0
        fake_pid = 424242

        class FakeProc:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        def fake_popen(cmd, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            time.sleep(0.15)
            state_path.write_text(
                json.dumps(
                    {
                        "pid": fake_pid,
                        "url": "http://127.0.0.1:18765/?mode=compact",
                        "executable": str(bin_path),
                    }
                ),
                encoding="utf-8",
            )
            return FakeProc(fake_pid)

        def fake_verify(pid: int, expected_executable: str | None = None) -> bool:
            return pid == fake_pid and state_path.is_file()

        results: list[dict] = []
        results_lock = threading.Lock()

        def worker() -> None:
            r = next_step_floating.launch_floating_hud(
                url="http://127.0.0.1:18765/",
                compact=True,
                data_dir=self.data_dir,
            )
            with results_lock:
                results.append(r)

        with (
            mock.patch.object(next_step_floating, "compile_floating_hud", return_value=bin_path),
            mock.patch.object(next_step_floating, "_verify_floating_hud_process", side_effect=fake_verify),
            mock.patch("subprocess.Popen", side_effect=fake_popen),
            mock.patch("os.kill") as mock_kill,
        ):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)

        self.assertEqual(spawn_count, 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.get("ok") for r in results))
        self.assertEqual(sorted(bool(r.get("alreadyRunning")) for r in results), [False, True])
        mock_kill.assert_called_once()

    def test_mcp_next_step_get_open_window_hook(self) -> None:
        """Verify next_step_get(open_window=True) calls launch_floating_hud and formats status in response."""
        fake_hud = {"ok": True, "url": "http://127.0.0.1:18765/", "port": 18765, "pid": 4321}
        fake_win = {"ok": True, "alreadyRunning": False, "pid": 9876, "url": "http://127.0.0.1:18765/?mode=compact"}
        with (
            mock.patch.object(hud_server, "ensure_hud_server_running", return_value=fake_hud),
            mock.patch.object(next_step_floating, "launch_floating_hud", return_value=fake_win) as mock_launch,
        ):
            res = mcp_server._handle_tool_call("next_step_get", {"ensure_hud": True, "open_window": True})
        self.assertFalse(res["isError"])
        mock_launch.assert_called_once_with(url="http://127.0.0.1:18765/", compact=True)
        txt = res["content"][0]["text"]
        self.assertIn("已打开悬浮窗", txt)
        self.assertIn("9876", txt)
        self.assertIn("?mode=compact", txt)

    def test_floating_hud_auto_start_hooks_and_dismiss_flow(self) -> None:
        """Option A: SessionStart/PreInvocation trigger detached --auto, respect dismiss marker & lock, and never disturb active window."""
        cid = "auto-start-flow-001"
        spawned_calls: list[tuple[list[str], dict]] = []

        class FakeProc:
            def __init__(self, pid: int) -> None:
                self.pid = pid

            def poll(self) -> None:
                return None

        def fake_popen(cmd, **kwargs):
            spawned_calls.append((list(cmd), dict(kwargs)))
            return FakeProc(70000 + len(spawned_calls))

        with (
            mock.patch.dict(os.environ, {"NEXT_STEP_NO_AUTO_FLOATING_HUD": ""}, clear=False),
            mock.patch.object(next_step_floating.sys, "platform", "darwin"),
            mock.patch.object(next_step_floating.subprocess, "Popen", side_effect=fake_popen),
        ):
            # 1. SessionStart triggers detached --auto launch
            hook.handle_session_start(self._base_payload(cid))
            self.assertEqual(len(spawned_calls), 1)
            cmd1, kw1 = spawned_calls[0]
            self.assertEqual(Path(cmd1[1]).resolve(), Path(next_step_floating.__file__).resolve())
            self.assertIn("--auto", cmd1)
            self.assertIn("--data-dir", cmd1)
            self.assertEqual(cmd1[cmd1.index("--data-dir") + 1], str(self.data_dir))
            self.assertEqual(kw1["stdin"], subprocess.DEVNULL)
            self.assertEqual(kw1["stdout"], subprocess.DEVNULL)
            self.assertEqual(kw1["stderr"], subprocess.DEVNULL)
            self.assertTrue(kw1["close_fds"])
            self.assertTrue(kw1["start_new_session"])

            # 2. PreInvocation also triggers detached --auto when neither running nor dismissed
            hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1))
            self.assertEqual(len(spawned_calls), 2)

            # 3. Manual close writes dismiss marker -> hooks & launch_floating_hud(respect_dismiss=True) must skip
            dismiss_path = next_step_floating.dismiss_marker_path(self.data_dir)
            dismiss_path.write_text(json.dumps({"dismissedAt": time.time(), "pid": 123, "reason": "window_closed"}), encoding="utf-8")

            hook.handle_session_start(self._base_payload(cid))
            hook.handle_pre_invocation(self._base_payload(cid, invocationNum=2))
            self.assertEqual(len(spawned_calls), 2)

            auto_res = next_step_floating.launch_floating_hud(compact=True, data_dir=self.data_dir, respect_dismiss=True)
            self.assertEqual(auto_res, {"ok": True, "skipped": True, "reason": "dismissed"})

            # 4. MCP restart / clear_dismiss=True removes dismiss marker and spawns detached --auto
            mcp_auto = next_step_floating.ensure_floating_hud_auto_started(
                data_dir=self.data_dir,
                clear_dismiss=True,
                reason="mcp_start",
            )
            self.assertTrue(mcp_auto["ok"])
            self.assertFalse(mcp_auto["skipped"])
            self.assertTrue(mcp_auto["spawned"])
            self.assertFalse(dismiss_path.exists())
            self.assertEqual(len(spawned_calls), 3)

            # 5. Held floating_hud.lock causes hook (clear_dismiss=False) to skip, while clear_dismiss=True still spawns
            lock_path = self.data_dir / "floating_hud.lock"
            with lock_path.open("a", encoding="utf-8") as held_fh:
                fcntl.flock(held_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    res_locked = next_step_floating.ensure_floating_hud_auto_started(
                        data_dir=self.data_dir,
                        clear_dismiss=False,
                        reason="session_start",
                    )
                    self.assertEqual(res_locked, {"ok": True, "skipped": True, "reason": "launch_in_progress"})
                    self.assertEqual(len(spawned_calls), 3)

                    res_mcp_locked = next_step_floating.ensure_floating_hud_auto_started(
                        data_dir=self.data_dir,
                        clear_dismiss=True,
                        reason="mcp_start",
                    )
                    self.assertTrue(res_mcp_locked["spawned"])
                    self.assertEqual(len(spawned_calls), 4)
                finally:
                    fcntl.flock(held_fh.fileno(), fcntl.LOCK_UN)

            # 6. Running --full window must NEVER be killed or signaled by auto-launch (even when auto wants compact)
            running_full = {"pid": 54321, "url": "http://127.0.0.1:18765/", "executable": "fake"}
            with (
                mock.patch.object(next_step_floating, "is_floating_hud_running", return_value=running_full),
                mock.patch("os.kill") as mock_kill,
            ):
                skip_running = next_step_floating.ensure_floating_hud_auto_started(
                    data_dir=self.data_dir,
                    clear_dismiss=False,
                    reason="pre_invocation",
                )
                self.assertEqual(
                    skip_running,
                    {"ok": True, "skipped": True, "reason": "already_running", "pid": 54321},
                )
                respect_running = next_step_floating.launch_floating_hud(
                    url="http://127.0.0.1:18765/",
                    compact=True,
                    data_dir=self.data_dir,
                    respect_dismiss=True,
                )
                self.assertTrue(respect_running["ok"])
                self.assertTrue(respect_running["alreadyRunning"])
                self.assertEqual(respect_running["pid"], 54321)
                self.assertEqual(respect_running["url"], "http://127.0.0.1:18765/")
                mock_kill.assert_not_called()

        # 7. Explicit launch (respect_dismiss=False) clears dismiss marker and passes --dismiss-file
        bin_path = self.data_dir / "bin" / "next-step-floating-hud"
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
        _, _, state_path, _ = next_step_floating._floating_paths(self.data_dir)
        dismiss_path = next_step_floating.dismiss_marker_path(self.data_dir)
        dismiss_path.write_text(json.dumps({"dismissedAt": time.time(), "pid": 999, "reason": "window_closed"}), encoding="utf-8")
        explicit_cmds: list[list[str]] = []

        def fake_explicit_popen(cmd, **kwargs):
            explicit_cmds.append(list(cmd))
            state_path.write_text(
                json.dumps(
                    {
                        "pid": 65432,
                        "url": "http://127.0.0.1:18765/?mode=compact",
                        "executable": str(bin_path),
                    }
                ),
                encoding="utf-8",
            )
            return FakeProc(65432)

        with (
            mock.patch.object(next_step_floating, "compile_floating_hud", return_value=bin_path),
            mock.patch.object(
                next_step_floating,
                "_verify_floating_hud_process",
                side_effect=lambda pid, expected_executable=None: pid == 65432 and state_path.is_file(),
            ),
            mock.patch.object(next_step_floating.subprocess, "Popen", side_effect=fake_explicit_popen),
        ):
            res_exp = next_step_floating.launch_floating_hud(
                url="http://127.0.0.1:18765/",
                compact=True,
                data_dir=self.data_dir,
                respect_dismiss=False,
            )
        self.assertTrue(res_exp["ok"])
        self.assertFalse(res_exp["alreadyRunning"])
        self.assertFalse(dismiss_path.exists())
        self.assertEqual(len(explicit_cmds), 1)
        self.assertIn("--dismiss-file", explicit_cmds[0])
        self.assertEqual(explicit_cmds[0][explicit_cmds[0].index("--dismiss-file") + 1], str(dismiss_path))

    def test_mcp_server_main_triggers_non_blocking_auto_start(self) -> None:
        """Verify mcp_server.main triggers non-blocking floating HUD auto-start once and never crashes on error."""
        with (
            mock.patch.object(mcp_server, "_read_message", return_value=(None, "jsonl")),
            mock.patch.object(
                mcp_server.next_step_floating,
                "ensure_floating_hud_auto_started",
                return_value={"ok": True, "spawned": True},
            ) as mock_auto,
        ):
            mcp_server.main()
        mock_auto.assert_called_once_with(
            data_dir=core.DATA_DIR,
            clear_dismiss=True,
            reason="mcp_start",
        )

        # Error from auto-start helper must be caught, logged, and never crash MCP server main loop
        with (
            mock.patch.object(mcp_server, "_read_message", return_value=(None, "jsonl")),
            mock.patch.object(
                mcp_server.next_step_floating,
                "ensure_floating_hud_auto_started",
                side_effect=RuntimeError("boom"),
            ),
        ):
            mcp_server.main()
        self.assertTrue(core.ERROR_LOG_PATH.is_file())
        self.assertIn("mcp_auto_start_floating_hud: boom", core.ERROR_LOG_PATH.read_text(encoding="utf-8"))

    def test_declared_next_step_survives_stop_hook_block_and_retry(self) -> None:
        """[P1 Regression] Explicit next_step_publish survives Stop-hook block + same-turn retry until a new user turn."""
        # Part 1: With transcript.jsonl tracking USER_EXPLICIT step
        cid_tr = "session-stop-retry-transcript"
        plan_path = self.art_dir / "retry_plan.md"
        plan_path.write_text("# Plan v1\n", encoding="utf-8")
        transcript_path = self.art_dir / "retry_transcript.jsonl"
        transcript_path.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "测第3A轮"}) + "\n",
            encoding="utf-8",
        )

        hook.handle_pre_invocation(
            self._base_payload(cid_tr, transcriptPath=str(transcript_path), invocationNum=1, lastUserInput="测第3A轮")
        )
        pub_args = {
            "next_action": "确认 v1 卡片后触发 v2 改写",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": "演示同轮改写为 v2（触发 🟡 已过期）",
            "plan_path": str(plan_path),
        }
        pre_out = hook.handle_pre_tool_use(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                stepIdx=3,
                toolCall={"id": "tc_pub", "name": "mcp_next-step_next_step_publish", "args": pub_args},
            )
        )
        mcp_server._handle_tool_call("next_step_publish", dict(pub_args, **pre_out["overwrite"], _ensure_hud=False))
        hook.handle_post_tool_use(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                stepIdx=3,
                toolCall={"id": "tc_pub", "name": "mcp_next-step_next_step_publish", "args": pub_args},
            )
        )
        hook.handle_stop(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                lastUserInput="测第3A轮",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )

        st_stop1 = core.load_session(cid_tr)
        self.assertEqual(st_stop1["sourceType"], "AGENT_DECLARED")
        self.assertTrue(st_stop1["waitingOnUser"])
        self.assertEqual(st_stop1["suggestedPrompt"], "演示同轮改写为 v2（触发 🟡 已过期）")
        self.assertIsNotNone(st_stop1["pendingDeclaration"])

        # Same-turn Stop-hook block retry (PreInvocation(2) + codex_reply + Stop #2 without re-calling next_step_publish)
        hook.handle_pre_invocation(
            self._base_payload(cid_tr, transcriptPath=str(transcript_path), invocationNum=2, lastUserInput="测第3A轮")
        )
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                stepIdx=6,
                toolCall={"id": "tc_reply", "name": "codex_reply", "args": {"session_id": "s1"}},
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                stepIdx=6,
                toolCall={"id": "tc_reply", "name": "codex_reply", "args": {"session_id": "s1"}},
            )
        )
        hook.handle_stop(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                lastUserInput="测第3A轮",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )

        st_stop2 = core.evaluate_staleness(core.load_session(cid_tr))
        self.assertEqual(st_stop2["sourceType"], "AGENT_DECLARED")
        self.assertTrue(st_stop2["waitingOnUser"])
        self.assertEqual(st_stop2["suggestedPrompt"], "演示同轮改写为 v2（触发 🟡 已过期）")
        self.assertIsNotNone(st_stop2["planArtifact"])
        self.assertFalse(st_stop2["stale"])

        # Third Stop-hook retry edits linked plan file -> Stop #3 keeps AGENT_DECLARED and marks stale=True
        hook.handle_pre_invocation(
            self._base_payload(cid_tr, transcriptPath=str(transcript_path), invocationNum=3, lastUserInput="测第3A轮")
        )
        plan_path.write_text("# Plan v2 edited during retry\n", encoding="utf-8")
        hook.handle_post_tool_use(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                stepIdx=8,
                toolCall={"id": "tc_edit", "name": "edit_file", "args": {"Path": str(plan_path)}},
            )
        )
        hook.handle_stop(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                lastUserInput="测第3A轮",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )
        st_stop3 = core.evaluate_staleness(core.load_session(cid_tr))
        self.assertEqual(st_stop3["sourceType"], "AGENT_DECLARED")
        self.assertTrue(st_stop3["stale"])
        self.assertTrue(st_stop3["planArtifact"]["stale"])

        # A real new user turn in transcript clears pendingDeclaration and ends as NONE if unpublished
        with transcript_path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"step_index": 10, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "纯问答结束"}) + "\n"
            )
        hook.handle_pre_invocation(
            self._base_payload(cid_tr, transcriptPath=str(transcript_path), invocationNum=0, lastUserInput="纯问答结束")
        )
        self.assertIsNone(core.load_session(cid_tr)["pendingDeclaration"])
        hook.handle_stop(
            self._base_payload(
                cid_tr,
                transcriptPath=str(transcript_path),
                lastUserInput="纯问答结束",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )
        st_new_turn = core.load_session(cid_tr)
        self.assertEqual(st_new_turn["sourceType"], "NONE")
        self.assertFalse(st_new_turn["waitingOnUser"])

        # Part 2: Without transcript.jsonl (non-existent transcriptPath), inv_num > last_end_inv preserves declaration
        cid_no_tr = "session-stop-retry-no-transcript"
        missing_tr = str(self.art_dir / "does_not_exist_transcript.jsonl")
        hook.handle_pre_invocation(
            self._base_payload(cid_no_tr, transcriptPath=missing_tr, invocationNum=1, lastUserInput="无transcript测试")
        )
        pub_no_tr = {
            "next_action": "验证无 transcript 时 Stop 重试不丢失",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": "下一步",
        }
        pre2 = hook.handle_pre_tool_use(
            self._base_payload(
                cid_no_tr,
                transcriptPath=missing_tr,
                stepIdx=2,
                toolCall={"id": "tc_pub2", "name": "mcp_next-step_next_step_publish", "args": pub_no_tr},
            )
        )
        mcp_server._handle_tool_call("next_step_publish", dict(pub_no_tr, **pre2["overwrite"], _ensure_hud=False))
        hook.handle_post_tool_use(
            self._base_payload(
                cid_no_tr,
                transcriptPath=missing_tr,
                stepIdx=2,
                toolCall={"id": "tc_pub2", "name": "mcp_next-step_next_step_publish", "args": pub_no_tr},
            )
        )
        hook.handle_stop(
            self._base_payload(
                cid_no_tr,
                transcriptPath=missing_tr,
                lastUserInput="无transcript测试",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )

        # Same-turn retry with inv_num=2 (> lastTurnEndInvocationNum=1) preserves declaration
        hook.handle_pre_invocation(
            self._base_payload(cid_no_tr, transcriptPath=missing_tr, invocationNum=2, lastUserInput="无transcript测试")
        )
        hook.handle_stop(
            self._base_payload(
                cid_no_tr,
                transcriptPath=missing_tr,
                lastUserInput="无transcript测试",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )
        st_no_tr_retry = core.load_session(cid_no_tr)
        self.assertEqual(st_no_tr_retry["sourceType"], "AGENT_DECLARED")
        self.assertEqual(st_no_tr_retry["suggestedPrompt"], "下一步")

        # Next real user turn starts at inv_num=0 (<= lastTurnEndInvocationNum=2) -> cleared to NONE
        hook.handle_pre_invocation(
            self._base_payload(cid_no_tr, transcriptPath=missing_tr, invocationNum=0, lastUserInput="无transcript测试")
        )
        hook.handle_stop(
            self._base_payload(
                cid_no_tr,
                transcriptPath=missing_tr,
                lastUserInput="无transcript测试",
                terminationReason="NO_TOOL_CALL",
                fullyIdle=True,
            )
        )
        self.assertEqual(core.load_session(cid_no_tr)["sourceType"], "NONE")

    def test_mid_turn_plan_rewrite_marks_session_stale_before_stop(self) -> None:
        """[Round 3B Bugfix] Rewriting linked plan mid-turn (LIFECYCLE/WORKING) immediately sets stale=True & disables #copyBtn before Stop."""
        cid = "session-mid-turn-stale"
        plan_path = self.art_dir / "mid_turn_plan.md"
        plan_path.write_text("# Plan v1\n", encoding="utf-8")

        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="测同轮改写"))
        pub_args = {
            "next_action": "绑定 v1 计划后立即同轮改写",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": "旧版 v1 指令",
            "plan_path": str(plan_path),
        }
        pre_out = hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                toolCall={"id": "tc_pub", "name": "mcp_next-step_next_step_publish", "args": pub_args},
            )
        )
        mcp_server._handle_tool_call("next_step_publish", dict(pub_args, **pre_out["overwrite"], _ensure_hud=False))
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=2,
                toolCall={"id": "tc_pub", "name": "mcp_next-step_next_step_publish", "args": pub_args},
            )
        )

        # Mid-turn write_file switches sourceType to LIFECYCLE / TOOL_RUNNING -> WORKING
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                toolCall={"id": "tc_w2", "name": "write_file", "args": {"Path": str(plan_path)}},
            )
        )
        plan_path.write_text("# Plan v2\n", encoding="utf-8")
        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=3,
                toolCall={"id": "tc_w2", "name": "write_file", "args": {"Path": str(plan_path)}},
            )
        )

        # Before Stop fires, persisted state, evaluate_staleness, and compact HUD JS must all reflect stale=True
        st_mid = core.load_session(cid)
        self.assertEqual(st_mid["sourceType"], "LIFECYCLE")
        self.assertEqual(st_mid["lifecycle"], "WORKING")
        self.assertTrue(st_mid["stale"])
        self.assertTrue(st_mid["planArtifact"]["stale"])

        ev_mid = core.evaluate_staleness(st_mid)
        self.assertEqual(ev_mid["sourceType"], "LIFECYCLE")
        self.assertTrue(ev_mid["stale"])
        self.assertTrue(ev_mid["planArtifact"]["stale"])

        html_compact = core.render_hud_html(compact=True)
        self.assertIn("cur.planArtifact && cur.planArtifact.stale", html_compact)
        self.assertIn("disabled title=", html_compact)

    def test_mcp_apps_protocol_resource_and_entrypoints(self) -> None:
        """Verify MCP Apps (SEP-1865 + Antigravity aux_pane) protocol: initialize, tools/_meta, resources/list, resources/read."""
        init_resp = mcp_server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2026-01-26",
                    "clientInfo": {"name": "Antigravity Test Host", "version": "2.34.0"},
                    "capabilities": {
                        "extensions": {
                            "io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]},
                            "com.google.antigravity/ui": {"entrypoints": ["aux_pane", "global", "file"]},
                        }
                    },
                },
            }
        )
        self.assertIsNotNone(init_resp)
        res_init = init_resp["result"]
        self.assertEqual(res_init["protocolVersion"], "2026-01-26")
        self.assertEqual(res_init["serverInfo"]["version"], "0.3.0")
        self.assertIn("tools", res_init["capabilities"])
        self.assertIn("resources", res_init["capabilities"])

        cap_file = self.data_dir / "mcp_host_capabilities.json"
        self.assertTrue(cap_file.is_file())
        recorded_caps = json.loads(cap_file.read_text(encoding="utf-8"))
        self.assertIn("io.modelcontextprotocol/ui", recorded_caps["extensions"])

        list_tools = mcp_server._dispatch({"jsonrpc": "2.0", "id": 11, "method": "tools/list", "params": {}})
        tools_by_name = {t["name"]: t for t in list_tools["result"]["tools"]}

        sp_tool = tools_by_name["next_step_side_panel"]
        self.assertEqual(sp_tool["_meta"]["ui"]["resourceUri"], "ui://next-step/side-panel.html")
        self.assertEqual(sp_tool["_meta"]["ui"]["visibility"], ["model", "app"])
        entrypoints = sp_tool["_meta"]["com.google.antigravity/ui"]["entrypoints"]
        self.assertEqual(entrypoints, [{"type": "aux_pane", "title": "Next Step HUD"}])
        self.assertFalse(any(ep.get("type") == "global" for ep in entrypoints))

        ps_tool = tools_by_name["next_step_panel_state"]
        self.assertEqual(ps_tool["_meta"]["ui"]["visibility"], ["app"])

        res_list = mcp_server._dispatch({"jsonrpc": "2.0", "id": 12, "method": "resources/list", "params": {}})
        self.assertEqual(len(res_list["result"]["resources"]), 1)
        r_meta = res_list["result"]["resources"][0]
        self.assertEqual(r_meta["uri"], "ui://next-step/side-panel.html")
        self.assertEqual(r_meta["mimeType"], "text/html;profile=mcp-app")

        res_read = mcp_server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 13,
                "method": "resources/read",
                "params": {"uri": "ui://next-step/side-panel.html"},
            }
        )
        content_item = res_read["result"]["contents"][0]
        self.assertEqual(content_item["uri"], "ui://next-step/side-panel.html")
        self.assertEqual(content_item["mimeType"], "text/html;profile=mcp-app")
        self.assertEqual(content_item["_meta"]["ui"]["prefersBorder"], False)
        html_doc = content_item["text"]
        self.assertTrue(html_doc.startswith("<!DOCTYPE html>"))
        self.assertLess(len(html_doc.encode("utf-8")), 10 * 1024 * 1024)
        self.assertFalse(re.search(r"<script[^>]+src=", html_doc, re.I))
        self.assertFalse(re.search(r"<link[^>]+href=[\"']https?:", html_doc, re.I))
        self.assertIn("next_step_panel_state", html_doc)

        missing_read = mcp_server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 14,
                "method": "resources/read",
                "params": {"uri": "ui://next-step/missing.html"},
            }
        )
        self.assertEqual(missing_read["error"]["code"], -32002)

    def test_side_panel_session_isolation_unbound_and_stale_enforcement(self) -> None:
        """Verify strict session isolation: manual {} stays unbound, hooks bind current session, read-only state refresh, and stale blocks copy."""
        cid_a = "session-side-panel-A"
        cid_b = "session-side-panel-B"
        cid_unrecorded = "session-side-panel-NEW"
        plan_b = self.art_dir / "panel_plan_b.md"
        plan_b.write_text("# Plan B v1\n", encoding="utf-8")

        # Session A: waiting on ask_question approval
        hook.handle_pre_invocation(self._base_payload(cid_a, invocationNum=1, lastUserInput="会话A任务"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_a,
                stepIdx=2,
                lastUserInput="会话A任务",
                toolCall={
                    "id": "tc_q_a",
                    "name": "ask_question",
                    "args": {"questions": [{"question": "选择方案", "options": ["选项1", "选项2"]}]},
                },
            )
        )
        hook.handle_stop(self._base_payload(cid_a, lastUserInput="会话A任务", terminationReason="NO_TOOL_CALL", fullyIdle=True))

        # Session B: AGENT_DECLARED with linked plan + suggested prompt
        hook.handle_pre_invocation(self._base_payload(cid_b, invocationNum=1, lastUserInput="会话B任务"))
        pub_b = {
            "next_action": "执行会话 B 的下一步",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": "继续会话 B",
            "plan_path": str(plan_b),
        }
        pre_b = hook.handle_pre_tool_use(
            self._base_payload(
                cid_b,
                stepIdx=3,
                lastUserInput="会话B任务",
                toolCall={"id": "tc_pub_b", "name": "mcp_next-step_next_step_publish", "args": pub_b},
            )
        )
        mcp_server._handle_tool_call("next_step_publish", dict(pub_b, **pre_b["overwrite"], _ensure_hud=False))
        hook.handle_post_tool_use(
            self._base_payload(
                cid_b,
                stepIdx=3,
                lastUserInput="会话B任务",
                toolCall={"id": "tc_pub_b", "name": "mcp_next-step_next_step_publish", "args": pub_b},
            )
        )
        hook.handle_stop(self._base_payload(cid_b, lastUserInput="会话B任务", terminationReason="NO_TOOL_CALL", fullyIdle=True))

        # 1. Manual launch with {} must stay UNBOUND and NEVER guess most recent session
        manual_res = mcp_server._handle_tool_call("next_step_side_panel", {})
        self.assertFalse(manual_res["isError"])
        manual_sc = manual_res["structuredContent"]
        self.assertEqual(manual_sc["bindingMode"], "unbound")
        self.assertIsNone(manual_sc["boundConversationId"])
        self.assertIsNone(manual_sc["selectedSession"])
        self.assertEqual(len(manual_sc["sessions"]), 2)
        self.assertIn("未绑定当前会话", manual_res["content"][0]["text"])

        # 2. PreToolUse for next_step_side_panel overrides spoofed cid_b with real cid_a and preserves Session A's APPROVAL_REQUIRED state
        for tname, tc_args in (
            ("mcp_next-step_next_step_side_panel", {"conversation_id": cid_b}),
            ("call_mcp_tool", {"ServerName": "next-step_next_step", "ToolName": "next_step_side_panel", "Arguments": {"conversation_id": cid_b}}),
        ):
            pre_panel = hook.handle_pre_tool_use(
                self._base_payload(cid_a, stepIdx=9, toolCall={"id": "tc_p", "name": tname, "args": tc_args})
            )
            self.assertEqual(pre_panel["decision"], "allow")
            eff_args = pre_panel["overwrite"]["Arguments"] if tname == "call_mcp_tool" else dict(tc_args, **pre_panel["overwrite"])
            self.assertEqual(eff_args["_sys_conversation_id"], cid_a)
            hook.handle_post_tool_use(
                self._base_payload(cid_a, stepIdx=9, toolCall={"id": "tc_p", "name": tname, "args": tc_args})
            )
            panel_a_res = mcp_server._handle_tool_call("next_step_side_panel", eff_args)
            sc_a = panel_a_res["structuredContent"]
            self.assertEqual(sc_a["bindingMode"], "hook_bound")
            self.assertEqual(sc_a["boundConversationId"], cid_a)
            self.assertEqual(sc_a["selectedSession"]["conversationId"], cid_a)
            self.assertEqual(sc_a["selectedSession"]["sourceType"], "APPROVAL_REQUIRED")
            self.assertEqual(sc_a["selectedSession"]["options"], ["选项1", "选项2"])
            self.assertNotIn("继续会话 B", json.dumps(sc_a["selectedSession"], ensure_ascii=False))

        # 3. Unrecorded/new conversation must return sessionNotFound=True without leaking A or B
        pre_new = hook.handle_pre_tool_use(
            self._base_payload(cid_unrecorded, stepIdx=1, toolCall={"id": "tc_new", "name": "mcp_next-step_next_step_side_panel", "args": {}})
        )
        new_res = mcp_server._handle_tool_call("next_step_side_panel", pre_new["overwrite"])
        new_sc = new_res["structuredContent"]
        self.assertEqual(new_sc["bindingMode"], "hook_bound")
        self.assertTrue(new_sc["sessionNotFound"])
        self.assertIsNone(new_sc["selectedSession"])

        # 4. App-only state query is strictly read-only and blocks copy when plan file is rewritten
        session_b_path = core.session_path(cid_b)
        mtime_before = session_b_path.stat().st_mtime_ns
        bytes_before = session_b_path.read_bytes()

        state_b_fresh = mcp_server._handle_tool_call("next_step_panel_state", {"conversation_id": cid_b})
        self.assertEqual(state_b_fresh["structuredContent"]["bindingMode"], "explicit")
        self.assertFalse(state_b_fresh["structuredContent"]["selectedSession"]["copyBlocked"])

        plan_b.write_text("# Plan B v2 (rewritten)\n", encoding="utf-8")
        state_b_stale = mcp_server._handle_tool_call("next_step_panel_state", {"conversation_id": cid_b})
        sc_b_stale = state_b_stale["structuredContent"]
        self.assertTrue(sc_b_stale["selectedSession"]["stale"])
        self.assertTrue(sc_b_stale["selectedSession"]["planArtifact"]["stale"])
        self.assertTrue(sc_b_stale["selectedSession"]["copyBlocked"])
        self.assertIn("已阻止直接复制", state_b_stale["content"][0]["text"])
        self.assertNotIn("继续会话 B", state_b_stale["content"][0]["text"])

        # Verify next_step_panel_state never wrote to session_b_path
        self.assertEqual(session_b_path.stat().st_mtime_ns, mtime_before)
        self.assertEqual(session_b_path.read_bytes(), bytes_before)

        # 5. Long suggested_prompt (>=4,000 chars) must remain unclipped so tail constraints are never silently lost
        tail_marker = "TAIL_CONSTRAINT_DO_NOT_DELETE"
        long_prompt = ("A" * 3999) + tail_marker
        self.assertEqual(len(long_prompt), 4028)
        cid_long = "session-side-panel-long"
        hook.handle_pre_invocation(self._base_payload(cid_long, invocationNum=1, lastUserInput="长提示词测试"))
        pub_long = {
            "next_action": "执行长指令校验",
            "actor": "user",
            "waiting_on_user": True,
            "suggested_prompt": long_prompt,
        }
        pre_long = hook.handle_pre_tool_use(
            self._base_payload(
                cid_long,
                stepIdx=2,
                lastUserInput="长提示词测试",
                toolCall={"id": "tc_pub_long", "name": "mcp_next-step_next_step_publish", "args": pub_long},
            )
        )
        mcp_server._handle_tool_call("next_step_publish", dict(pub_long, **pre_long["overwrite"], _ensure_hud=False))
        hook.handle_post_tool_use(
            self._base_payload(
                cid_long,
                stepIdx=2,
                lastUserInput="长提示词测试",
                toolCall={"id": "tc_pub_long", "name": "mcp_next-step_next_step_publish", "args": pub_long},
            )
        )
        hook.handle_stop(self._base_payload(cid_long, lastUserInput="长提示词测试", terminationReason="NO_TOOL_CALL", fullyIdle=True))

        long_res = mcp_server._handle_tool_call("next_step_panel_state", {"conversation_id": cid_long})
        long_item = long_res["structuredContent"]["selectedSession"]
        self.assertFalse(long_item["copyBlocked"])
        self.assertEqual(long_item["suggestedPrompt"], long_prompt)
        self.assertTrue(long_item["suggestedPrompt"].endswith(tail_marker))

    def test_side_panel_built_bundle_vm_behavior(self) -> None:
        """Run the Node VM verification harness against the built single-file ui/side-panel.html."""
        node_bin = shutil.which("node")
        if node_bin is None:
            self.skipTest("node runtime unavailable")
        script_path = Path(__file__).resolve().parent / "verify_side_panel.mjs"
        self.assertTrue(script_path.is_file())
        proc = subprocess.run(
            [node_bin, str(script_path)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"verify_side_panel.mjs failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("ALL SIDE PANEL BUNDLE CHECKS PASSED", proc.stdout)


if __name__ == "__main__":
    unittest.main()
