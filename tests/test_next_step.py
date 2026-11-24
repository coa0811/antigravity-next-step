#!/usr/bin/env python3
"""Regression and integration test suite for the Antigravity Next Step HUD plugin (Route A)."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import next_step_core as core  # noqa: E402
import next_step_hook as hook  # noqa: E402
import next_step_mcp_server as mcp_server  # noqa: E402


class NextStepPluginTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp_data = tempfile.TemporaryDirectory()
        self._tmp_ws = tempfile.TemporaryDirectory()
        self._tmp_art = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp_data.name).resolve()
        self.ws_dir = Path(self._tmp_ws.name).resolve()
        self.art_dir = Path(self._tmp_art.name).resolve()
        core.set_data_dir(self.data_dir)
        core.ensure_data_dirs()

    def tearDown(self) -> None:
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

        # 3. MCP server executes next_step_publish
        mcp_args = dict(pub_args, **pre_out["overwrite"])
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

    def test_mcp_dispatch_publish_and_get_markdown(self) -> None:
        """Verify MCP JSON-RPC initialize/tools/call for next_step_publish and next_step_get Markdown without daemons."""
        init_resp = mcp_server._dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-01-26"}}
        )
        self.assertIsNotNone(init_resp)
        self.assertEqual(init_resp["result"]["serverInfo"]["name"], "next-step")
        self.assertEqual(init_resp["result"]["serverInfo"]["version"], "0.4.0")
        self.assertIn("resources", init_resp["result"]["capabilities"])

        list_resp = mcp_server._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_by_name = {t["name"]: t for t in list_resp["result"]["tools"]}
        self.assertEqual(
            set(tools_by_name),
            {"next_step_publish", "next_step_get", "next_step_side_panel", "next_step_panel_state"},
        )
        get_props = tools_by_name["next_step_get"]["inputSchema"]["properties"]
        self.assertNotIn("ensure_hud", get_props)
        self.assertNotIn("open_window", get_props)

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
                        "next_action": "在 Side Panel 中验证会话状态渲染",
                        "actor": "user",
                        "waiting_on_user": True,
                        "suggested_prompt": "验证通过，继续下一步",
                    },
                },
            }
        )
        self.assertFalse(call_resp["result"]["isError"])
        pub_text = call_resp["result"]["content"][0]["text"]
        self.assertIn("next_step_side_panel", pub_text)
        self.assertNotIn("127.0.0.1", pub_text)

        get_resp = mcp_server._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "next_step_get",
                    "arguments": {"conversation_id": cid},
                },
            }
        )
        self.assertFalse(get_resp["result"]["isError"])
        get_text = get_resp["result"]["content"][0]["text"]
        self.assertIn("在 Side Panel 中验证会话状态渲染", get_text)
        self.assertIn("验证通过，继续下一步", get_text)
        self.assertNotIn("127.0.0.1", get_text)

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
        mcp_server._handle_tool_call("next_step_publish", dict(pub_args, **pre_out["overwrite"]))
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
        mcp_server._handle_tool_call("next_step_publish", dict(pub_no_tr, **pre2["overwrite"]))
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
        mcp_server._handle_tool_call("next_step_publish", dict(pub_args, **pre_out["overwrite"]))
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

        panel_mid = core.build_side_panel_payload(bound_cid=st_mid["conversationId"], binding_mode="hook_bound")
        self.assertTrue(panel_mid["selectedSession"]["stale"])
        self.assertTrue(panel_mid["selectedSession"]["copyBlocked"])

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
        self.assertEqual(res_init["serverInfo"]["version"], "0.4.0")
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
        mcp_server._handle_tool_call("next_step_publish", dict(pub_b, **pre_b["overwrite"]))
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
        mcp_server._handle_tool_call("next_step_publish", dict(pub_long, **pre_long["overwrite"]))
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

    def test_multi_question_ask_question_preserves_all_questions_and_options(self) -> None:
        """Multi-question ask_question should preserve all questions/options and clear them on resolution."""
        cid = "conv-multi-q"
        hook.handle_pre_invocation(self._base_payload(cid, invocationNum=1, lastUserInput="configure db"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid,
                stepIdx=10,
                toolCall={
                    "id": "tc_multi_q",
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {
                                "question": "Choose database engine?",
                                "options": ["SQLite", "PostgreSQL"],
                            },
                            {
                                "question": "Enable migration guard?",
                                "options": ["Strict mode", "Skip check"],
                            },
                        ]
                    },
                },
            )
        )
        st = core.load_session(cid)
        self.assertEqual(st["lifecycle"], "WAITING_USER_DECISION")
        self.assertEqual(st["sourceType"], "APPROVAL_REQUIRED")
        self.assertTrue(st["waitingOnUser"])
        self.assertEqual(len(st["questions"]), 2)
        self.assertEqual(st["questions"][0]["options"], ["SQLite", "PostgreSQL"])
        self.assertEqual(st["questions"][1]["options"], ["Strict mode", "Skip check"])
        q_dump = json.dumps(st["questions"], ensure_ascii=False)
        self.assertIn("Choose database engine?", q_dump)
        self.assertIn("Enable migration guard?", q_dump)
        joined_opts = "\n".join(st["options"])
        self.assertIn("[Q1]", joined_opts)
        self.assertIn("[Q2]", joined_opts)
        self.assertIn("SQLite", joined_opts)
        self.assertIn("Skip check", joined_opts)

        md = core.format_session_markdown(st)
        self.assertIn("Choose database engine?", md)
        self.assertIn("Enable migration guard?", md)
        self.assertIn("PostgreSQL", md)
        self.assertIn("Strict mode", md)

        panel = core.build_side_panel_payload(bound_cid=cid, binding_mode="hook_bound")
        self.assertEqual(len(panel["selectedSession"]["questions"]), 2)
        panel_text = core.format_side_panel_text(panel)
        self.assertIn("Choose database engine?", panel_text)
        self.assertIn("Enable migration guard?", panel_text)

        hook.handle_post_tool_use(
            self._base_payload(
                cid,
                stepIdx=10,
                toolCall={"id": "tc_multi_q", "name": "ask_question", "args": {}},
            )
        )
        st_cleared = core.load_session(cid)
        self.assertEqual(st_cleared["questions"], [])
        self.assertEqual(st_cleared["options"], [])
        self.assertFalse(st_cleared["waitingOnUser"])

    def test_transcript_tail_window_reverse_read_large_file(self) -> None:
        """_load_transcript_steps should read from EOF tail window and expand for oversized trailing steps."""
        self.assertEqual(hook.TRANSCRIPT_INITIAL_TAIL_BYTES, 131072)
        cid = "conv-large-tr"
        tr_dir = self.art_dir / ".system_generated" / "logs"
        tr_dir.mkdir(parents=True, exist_ok=True)
        tr_path = tr_dir / "transcript.jsonl"

        early_pad = "X" * 600
        lines = [
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "OLD_TURN"})
        ]
        lines.extend(
            json.dumps({"step_index": i, "type": "PLANNER_RESPONSE", "content": f"EARLY_PADDING_{i:04d}_{early_pad}"})
            for i in range(1, 595)
        )
        lines.append(
            json.dumps({"step_index": 595, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "RECENT_TURN_USER"})
        )
        lines.append(
            json.dumps({"step_index": 596, "type": "PLANNER_RESPONSE", "content": "TAIL_MARKER_LAST_STEP"})
        )
        tr_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertGreater(tr_path.stat().st_size, 2 * hook.TRANSCRIPT_INITIAL_TAIL_BYTES)

        steps = hook._load_transcript_steps({"transcriptPath": str(tr_path)}, core.load_session(cid))
        self.assertIsInstance(steps, list)
        self.assertGreater(len(steps), 0)
        self.assertLess(len(steps), 597)
        self.assertEqual(steps[-1]["content"], "TAIL_MARKER_LAST_STEP")

        # Trailing step larger than initial 128KB window triggers reverse window expansion cleanly
        big_tr_path = tr_dir / "transcript_big_tail.jsonl"
        oversized_payload = "Y" * (hook.TRANSCRIPT_INITIAL_TAIL_BYTES + 32768)
        big_tr_path.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "HEAD"}) + "\n"
            + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": f"BIG_TAIL_END_MARKER_{oversized_payload}"}) + "\n",
            encoding="utf-8",
        )
        big_steps = hook._load_transcript_steps({"transcriptPath": str(big_tr_path)}, core.load_session(cid))
        self.assertEqual(len(big_steps), 2)
        self.assertTrue(big_steps[-1]["content"].startswith("BIG_TAIL_END_MARKER_"))

    def test_plan_mtime_fast_path_and_session_scan_cap(self) -> None:
        """Settled plan stat skips SHA-256 re-hash, edits re-hash & mark stale, and list_all_sessions caps at MAX_SCANNED_SESSIONS."""
        import os
        from unittest import mock

        cid = "conv-stat-fastpath"
        plan_path = self.art_dir / "implementation_plan.md"
        plan_path.write_text("# Plan v1\nInitial settled content.\n", encoding="utf-8")
        old_ts = time.time() - 15.0
        os.utime(plan_path, (old_ts, old_ts))

        base_payload = self._base_payload(cid)
        entry = core.build_plan_artifact_entry(
            str(plan_path),
            workspace_paths=base_payload.get("workspacePaths", []),
            artifact_dir=str(self.art_dir),
        )
        self.assertIsNotNone(entry)
        self.assertIn("mtimeNs", entry)
        self.assertIn("sizeBytes", entry)
        self.assertIn("inode", entry)
        self.assertEqual(len(entry["sha256"]), 64)

        st = core.load_session(cid)
        st["planArtifact"] = entry
        st["lifecycle"] = "WAITING_USER_DECISION"
        st["sourceType"] = "APPROVAL_REQUIRED"
        st["waitingOnUser"] = True
        st["actor"] = "user"
        st["nextAction"] = "Approve plan"
        st["suggestedPrompt"] = "Proceed"

        with mock.patch.object(core, "compute_file_sha256", wraps=core.compute_file_sha256) as spy_sha:
            ev_unchanged = core.evaluate_staleness(st)
            self.assertEqual(spy_sha.call_count, 0)
            self.assertFalse(ev_unchanged["stale"])

            # Mutate content -> size/mtime changes -> forces SHA-256 recompute & marks stale
            plan_path.write_text("# Plan v2\nModified content that invalidates prompt.\n", encoding="utf-8")
            ev_changed = core.evaluate_staleness(st)
            self.assertGreaterEqual(spy_sha.call_count, 1)
            self.assertTrue(ev_changed["stale"])
            self.assertTrue(ev_changed["planArtifact"]["stale"])

        # Populate 65 sessions with monotonic mtimes; verify MAX_SCANNED_SESSIONS=60 keeps newest 60 first
        self.assertEqual(core.MAX_SCANNED_SESSIONS, 60)
        core.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        base_ts = time.time() - 1000.0
        for idx in range(65):
            scid = f"conv-cap-{idx:03d}"
            sdata = core.load_session(scid)
            ts = base_ts + idx
            sdata["updatedAt"] = ts
            spath = core.session_path(scid)
            spath.write_text(json.dumps(sdata), encoding="utf-8")
            os.utime(spath, (ts, ts))

        scanned = core.list_all_sessions()
        self.assertEqual(len(scanned), 60)
        scanned_ids = {item["conversationId"] for item in scanned}
        self.assertEqual(scanned[0]["conversationId"], "conv-cap-064")
        self.assertNotIn("conv-cap-000", scanned_ids)
        self.assertNotIn("conv-cap-004", scanned_ids)

    def test_astra_consensus_isolation_scan_workspace_and_untruncated_options(self) -> None:
        """Cover all 7 Codex Astra consensus criteria: full prefix scan, summary/detail split, missing host ID, <=60 read budget, workspace path keys, and untruncated multi-question options."""
        import os
        from pathlib import Path as _Path
        from unittest import mock

        # 1 + 4. Populate 250 sessions to test prefix ambiguity across cap & <=60 JSON read budget
        core.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        base_ts = time.time() - 5000.0
        for idx in range(250):
            if idx == 5:
                cid = "abcd-old"
            elif idx == 6:
                cid = "uniq99-old"
            elif idx == 7:
                cid = "zcorrupt-valid"
            elif 10 <= idx < 20:
                cid = f"amany-{idx:02d}"
            elif idx == 240:
                cid = "abcd-new"
            else:
                cid = f"bulk-{idx:03d}"
            sdata = core.load_session(cid)
            ts = base_ts + idx
            sdata["updatedAt"] = ts
            sdata["workspacePaths"] = [f"/projects/bulk_{idx % 3}"]
            spath = core.session_path(cid)
            spath.write_text(json.dumps(sdata), encoding="utf-8")
            os.utime(spath, (ts, ts))

        corrupt_path = core.session_path("zcorrupt-bad")
        corrupt_path.write_text("{corrupt json", encoding="utf-8")
        os.utime(corrupt_path, (base_ts + 8, base_ts + 8))

        orig_read_text = _Path.read_text

        def _count_session_reads(spy_self: _Path, *args: object, **kwargs: object) -> str:
            if spy_self.parent == core.SESSIONS_DIR and spy_self.suffix == ".json":
                read_calls.append(spy_self.name)
            return orig_read_text(spy_self, *args, **kwargs)

        # Prefix colliding between outside-60 abcd-old and inside-60 abcd-new must stay unbound/ambiguous
        res_ambig = core.build_side_panel_payload(bound_cid="abcd")
        self.assertEqual(res_ambig["bindingMode"], "unbound")
        self.assertTrue(res_ambig["ambiguousPrefix"])
        self.assertIsNone(res_ambig["selectedSession"])

        # Prefix matching 1 valid + 1 corrupt file must refuse to confirm unique
        res_corrupt = core.build_side_panel_payload(bound_cid="zcorrupt")
        self.assertEqual(res_corrupt["bindingMode"], "unbound")
        self.assertTrue(res_corrupt["ambiguousPrefix"])
        self.assertIsNone(res_corrupt["selectedSession"])

        # Unique old session outside top-60 scan window still binds cleanly by prefix
        res_uniq = core.build_side_panel_payload(bound_cid="uniq99")
        self.assertEqual(res_uniq["bindingMode"], "explicit")
        self.assertFalse(res_uniq["ambiguousPrefix"])
        self.assertEqual(res_uniq["selectedSession"]["conversationId"], "uniq99-old")

        # Hard <=60 JSON read budget even with zero-matching workspace filter and limit=500; prefix <=2 extra reads
        read_calls: list[str] = []
        with mock.patch.object(_Path, "read_text", autospec=True, side_effect=_count_session_reads):
            list_zero = core.list_all_sessions(workspace_filter="/nonexistent/ws", max_sessions=500)
            self.assertEqual(list_zero, [])
            self.assertLessEqual(len(read_calls), 60)

            read_calls.clear()
            panel_zero = core.build_side_panel_payload(workspace_filter="/nonexistent/ws", limit=500)
            self.assertEqual(panel_zero["sessions"], [])
            self.assertLessEqual(len(read_calls), 60)

            read_calls.clear()
            panel_many = core.build_side_panel_payload(bound_cid="amany", workspace_filter="/nonexistent/ws", limit=500)
            self.assertEqual(panel_many["bindingMode"], "unbound")
            self.assertTrue(panel_many["ambiguousPrefix"])
            self.assertLessEqual(len(read_calls), 62)

        # 2. Summary vs Detail split: Session B secrets never leak into {} or Session A response
        cid_a = "conv-secret-a"
        cid_b = "conv-secret-b"
        ws_proj_foo = "/tmp/ws_collision/projects/foo"
        ws_work_foo = "/tmp/ws_collision/work/foo"
        plan_b_secret = self.art_dir / "secret_b_plan.md"
        plan_b_secret.write_text("# Secret B Plan\n", encoding="utf-8")

        hook.handle_pre_invocation(self._base_payload(cid_a, workspacePaths=[ws_proj_foo], lastUserInput="task A"))
        hook.handle_stop(self._base_payload(cid_a, workspacePaths=[ws_proj_foo], terminationReason="NO_TOOL_CALL", fullyIdle=True))

        hook.handle_pre_invocation(self._base_payload(cid_b, workspacePaths=[ws_work_foo], lastUserInput="task B"))
        pub_b_secret = {
            "next_action": "执行 B 专属动作",
            "detail": "UNIQUE_B_DETAIL_SECRET",
            "suggested_prompt": "UNIQUE_B_PROMPT_SECRET",
            "plan_path": str(plan_b_secret),
            "actor": "user",
            "waiting_on_user": True,
        }
        pre_bs = hook.handle_pre_tool_use(
            self._base_payload(
                cid_b,
                workspacePaths=[ws_work_foo],
                stepIdx=3,
                toolCall={"id": "tc_bs", "name": "mcp_next-step_next_step_publish", "args": pub_b_secret},
            )
        )
        mcp_server._handle_tool_call("next_step_publish", dict(pub_b_secret, **pre_bs["overwrite"]))
        hook.handle_post_tool_use(
            self._base_payload(
                cid_b,
                workspacePaths=[ws_work_foo],
                stepIdx=3,
                toolCall={"id": "tc_bs", "name": "mcp_next-step_next_step_publish", "args": pub_b_secret},
            )
        )
        hook.handle_stop(self._base_payload(cid_b, workspacePaths=[ws_work_foo], terminationReason="NO_TOOL_CALL", fullyIdle=True))
        # Also record a multi-question B variant with secret Q2 question & options
        cid_b_q = "conv-secret-bq"
        hook.handle_pre_invocation(self._base_payload(cid_b_q, workspacePaths=[], lastUserInput="task B Q"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_b_q,
                workspacePaths=[],
                stepIdx=4,
                toolCall={
                    "id": "tc_bq",
                    "name": "ask_question",
                    "args": {
                        "questions": [
                            {"question": "Confirm B mode?", "options": ["UNIQUE_B_OPT_SECRET"]},
                            {"question": "UNIQUE_B_QUESTION_SECRET", "options": ["UNIQUE_B_OPT2_SECRET"]},
                        ]
                    },
                },
            )
        )

        unbound_dump = json.dumps(mcp_server._handle_tool_call("next_step_side_panel", {})["structuredContent"], ensure_ascii=False)
        bound_a_dump = json.dumps(
            mcp_server._handle_tool_call("next_step_side_panel", {"_sys_conversation_id": cid_a})["structuredContent"],
            ensure_ascii=False,
        )
        for marker in (
            "UNIQUE_B_PROMPT_SECRET",
            "UNIQUE_B_DETAIL_SECRET",
            "UNIQUE_B_OPT_SECRET",
            "UNIQUE_B_OPT2_SECRET",
            "UNIQUE_B_QUESTION_SECRET",
            "secret_b_plan.md",
        ):
            self.assertNotIn(marker, unbound_dump)
            self.assertNotIn(marker, bound_a_dump)

        # 3. Missing host conversationId in PreToolUse/PostToolUse never creates default session or hook_bound
        default_json = core.session_path("default")
        self.assertFalse(default_json.exists())

        pre_panel_empty = hook.handle_pre_tool_use({"toolCall": {"id": "tc_pe", "name": "mcp_next-step_next_step_side_panel", "args": {}}})
        self.assertNotIn("_sys_conversation_id", pre_panel_empty.get("overwrite", {}))
        hook.handle_post_tool_use({"toolCall": {"id": "tc_pe", "name": "mcp_next-step_next_step_side_panel", "args": {}}})
        panel_empty_res = mcp_server._handle_tool_call("next_step_side_panel", pre_panel_empty.get("overwrite", {}))
        self.assertEqual(panel_empty_res["structuredContent"]["bindingMode"], "unbound")
        self.assertFalse(default_json.exists())

        pre_panel_exp = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_px", "name": "mcp_next-step_next_step_side_panel", "args": {"conversation_id": cid_a}}}
        )
        self.assertNotIn("_sys_conversation_id", pre_panel_exp.get("overwrite", {}))
        panel_exp_res = mcp_server._handle_tool_call(
            "next_step_side_panel",
            dict({"conversation_id": cid_a}, **pre_panel_exp.get("overwrite", {})),
        )
        self.assertEqual(panel_exp_res["structuredContent"]["bindingMode"], "explicit")
        self.assertFalse(default_json.exists())

        pub_unattr = {"next_action": "Unattributed publish", "actor": "user", "waiting_on_user": True}
        pre_pub_no_id = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_pu", "name": "mcp_next-step_next_step_publish", "args": pub_unattr}}
        )
        self.assertNotIn("_sys_conversation_id", pre_pub_no_id.get("overwrite", {}))
        hook.handle_post_tool_use({"toolCall": {"id": "tc_pu", "name": "mcp_next-step_next_step_publish", "args": pub_unattr}})
        self.assertFalse(default_json.exists())
        self.assertFalse(core.LATEST_PENDING_PATH.exists())

        # 5. Workspace canonical keys, disambiguated colliding labels, & distinct __none__ key
        ws_panel = core.build_side_panel_payload(limit=30)
        opts_by_key = {o["key"]: o["label"] for o in ws_panel["workspaceOptions"]}
        key_proj = core.normalize_workspace_path(ws_proj_foo)
        key_work = core.normalize_workspace_path(ws_work_foo)
        self.assertIn(key_proj, opts_by_key)
        self.assertIn(key_work, opts_by_key)
        self.assertIn("__none__", opts_by_key)
        self.assertNotIn("", opts_by_key)
        self.assertNotEqual(opts_by_key[key_proj], opts_by_key[key_work])
        self.assertIn("projects/foo", opts_by_key[key_proj])
        self.assertIn("work/foo", opts_by_key[key_work])

        # 6. >8 questions, >12 options, and >300-char option with tail marker preserved verbatim in panel & Markdown
        tail_marker = "OPT_TAIL_GUARD_99"
        long_opt = ("Z" * 340) + tail_marker
        many_questions = [
            {"question": f"Q{i} prompt?", "options": [f"Q{i}-A", f"Q{i}-B"]}
            for i in range(1, 10)
        ]
        many_questions.append(
            {
                "question": "Q10 final prompt?",
                "options": [f"Choice-{j}" for j in range(1, 14)] + [long_opt],
            }
        )
        cid_mq = "conv-many-q-unclipped"
        hook.handle_pre_invocation(self._base_payload(cid_mq, workspacePaths=[ws_proj_foo], lastUserInput="many questions"))
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_mq,
                workspacePaths=[ws_proj_foo],
                stepIdx=12,
                toolCall={"id": "tc_mq10", "name": "ask_question", "args": {"questions": many_questions}},
            )
        )
        st_mq = core.load_session(cid_mq)
        panel_mq = core.build_side_panel_payload(bound_cid=cid_mq, binding_mode="hook_bound")
        sel_mq = panel_mq["selectedSession"]
        self.assertEqual(len(sel_mq["questions"]), 10)
        self.assertEqual(len(sel_mq["questions"][9]["options"]), 14)
        self.assertEqual(sel_mq["questions"][9]["options"][13], long_opt)
        self.assertTrue(sel_mq["questions"][9]["options"][13].endswith(tail_marker))
        self.assertIn(f"[Q10] {long_opt}", sel_mq["options"])
        md_mq = core.format_session_markdown(st_mq)
        txt_mq = core.format_side_panel_text(panel_mq)
        self.assertIn(tail_marker, md_mq)
        self.assertIn(tail_marker, txt_mq)

        # 7. Round 3 regressions: hook_bound exact-only, spoofed _sys_conversation_id strip, /work vs /work/foo exact workspace match, next_step_get prefix lookup, & unattributed next_step_publish error
        hook.handle_pre_invocation(self._base_payload("team-fork", workspacePaths=["/tmp/ws_exact/work/foo"], lastUserInput="forked"))
        hook.handle_stop(self._base_payload("team-fork", workspacePaths=["/tmp/ws_exact/work/foo"], terminationReason="NO_TOOL_CALL", fullyIdle=True))
        res_hook_exact = core.build_side_panel_payload(bound_cid="team", binding_mode="hook_bound")
        self.assertEqual(res_hook_exact["bindingMode"], "hook_bound")
        self.assertEqual(res_hook_exact["boundConversationId"], "team")
        self.assertTrue(res_hook_exact["sessionNotFound"])
        self.assertIsNone(res_hook_exact["selectedSession"])
        res_explicit_team = core.build_side_panel_payload(bound_cid="team", binding_mode="explicit")
        self.assertEqual(res_explicit_team["bindingMode"], "explicit")
        self.assertEqual(res_explicit_team["selectedSession"]["conversationId"], "team-fork")

        # Missing host conversationId with spoofed _sys_conversation_id across direct & call_mcp_tool paths
        spoof_panel_direct = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_spd", "name": "mcp_next-step_next_step_side_panel", "args": {"_sys_conversation_id": cid_a}}}
        )
        merged_spd = dict({"_sys_conversation_id": cid_a}, **spoof_panel_direct.get("overwrite", {}))
        res_spd = mcp_server._handle_tool_call("next_step_side_panel", merged_spd)
        self.assertEqual(res_spd["structuredContent"]["bindingMode"], "unbound")
        self.assertIsNone(res_spd["structuredContent"]["selectedSession"])

        spoof_panel_exp = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_spx", "name": "mcp_next-step_next_step_side_panel", "args": {"_sys_conversation_id": cid_b, "conversation_id": cid_a}}}
        )
        merged_spx = dict({"_sys_conversation_id": cid_b, "conversation_id": cid_a}, **spoof_panel_exp.get("overwrite", {}))
        res_spx = mcp_server._handle_tool_call("next_step_side_panel", merged_spx)
        self.assertEqual(res_spx["structuredContent"]["bindingMode"], "explicit")
        self.assertEqual(res_spx["structuredContent"]["boundConversationId"], cid_a)

        spoof_panel_wrap = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_spw", "name": "call_mcp_tool", "args": {"ServerName": "next-step_next_step", "ToolName": "next_step_side_panel", "Arguments": {"_sys_conversation_id": cid_a}}}}
        )
        self.assertNotIn("_sys_conversation_id", spoof_panel_wrap["overwrite"]["Arguments"])
        res_spw = mcp_server._handle_tool_call("next_step_side_panel", spoof_panel_wrap["overwrite"]["Arguments"])
        self.assertEqual(res_spw["structuredContent"]["bindingMode"], "unbound")

        st_a_before = core.load_session(cid_a)
        spoof_pub_direct = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_pubd", "name": "mcp_next-step_next_step_publish", "args": {"_sys_conversation_id": cid_a, "next_action": "spoofed!"}}}
        )
        self.assertEqual(spoof_pub_direct["overwrite"]["_sys_conversation_id"], "")
        res_pubd = mcp_server._handle_tool_call("next_step_publish", dict({"_sys_conversation_id": cid_a, "next_action": "spoofed!"}, **spoof_pub_direct["overwrite"]))
        self.assertTrue(res_pubd["isError"])
        self.assertEqual(core.load_session(cid_a), st_a_before)

        spoof_pub_wrap = hook.handle_pre_tool_use(
            {"toolCall": {"id": "tc_pubw", "name": "call_mcp_tool", "args": {"ServerName": "next-step_next_step", "ToolName": "next_step_publish", "Arguments": {"_sys_conversation_id": cid_a, "next_action": "spoofed!"}}}}
        )
        self.assertNotIn("_sys_conversation_id", spoof_pub_wrap["overwrite"]["Arguments"])
        self.assertEqual(core.load_session(cid_a), st_a_before)

        # /work vs /work/foo exact workspace separation in list_all_sessions
        hook.handle_pre_invocation(self._base_payload("ws-parent-only", workspacePaths=["/tmp/ws_exact/work"], lastUserInput="parent"))
        hook.handle_stop(self._base_payload("ws-parent-only", workspacePaths=["/tmp/ws_exact/work"], terminationReason="NO_TOOL_CALL", fullyIdle=True))
        child_only = core.list_all_sessions(workspace_filter="/tmp/ws_exact/work/foo")
        self.assertEqual([s["conversationId"] for s in child_only], ["team-fork"])
        parent_only = core.list_all_sessions(workspace_filter="/tmp/ws_exact/work")
        self.assertEqual([s["conversationId"] for s in parent_only], ["ws-parent-only"])

        # next_step_get uses full-dir prefix resolution & refuses ambiguous matches
        get_old = mcp_server._handle_tool_call("next_step_get", {"conversation_id": "uniq99"})
        self.assertFalse(get_old["isError"])
        self.assertIn("uniq99-old", get_old["content"][0]["text"])
        get_ambig = mcp_server._handle_tool_call("next_step_get", {"conversation_id": "abcd"})
        self.assertIn("匹配多个会话", get_ambig["content"][0]["text"])

        # Unattributed next_step_publish performs 0 writes & returns explicit isError=True
        sessions_snapshot_before = sorted(p.name for p in core.SESSIONS_DIR.glob("*.json"))
        orphan_pub = mcp_server._handle_tool_call("next_step_publish", {"next_action": "Should not publish"})
        self.assertTrue(orphan_pub["isError"])
        self.assertIn("缺少会话归属", orphan_pub["content"][0]["text"])
        self.assertNotIn("pending-", orphan_pub["content"][0]["text"])
        self.assertEqual(sessions_snapshot_before, sorted(p.name for p in core.SESSIONS_DIR.glob("*.json")))
        self.assertFalse(core.LATEST_PENDING_PATH.exists())
        self.assertFalse(default_json.exists())

        # 8. Round 4 regressions: app-only next_step_panel_state exact-only lookup & dual-path primary workspace matching
        state_team = mcp_server._handle_tool_call("next_step_panel_state", {"conversation_id": "team"})
        sc_team = state_team["structuredContent"]
        self.assertEqual(sc_team["boundConversationId"], "team")
        self.assertTrue(sc_team["sessionNotFound"])
        self.assertIsNone(sc_team["selectedSession"])

        ws_pri = "/tmp/ws_multi/primary_a"
        ws_sec = "/tmp/ws_multi/secondary_b"
        hook.handle_pre_invocation(self._base_payload("ws-multi-ab", workspacePaths=[ws_pri, ws_sec], lastUserInput="multi ws"))
        hook.handle_stop(self._base_payload("ws-multi-ab", workspacePaths=[ws_pri, ws_sec], terminationReason="NO_TOOL_CALL", fullyIdle=True))
        pri_list = core.list_all_sessions(workspace_filter=ws_pri)
        pri_panel = core.build_side_panel_payload(workspace_filter=ws_pri)
        self.assertEqual([s["conversationId"] for s in pri_list], ["ws-multi-ab"])
        self.assertEqual(len(pri_panel["sessions"]), 1)
        self.assertEqual(pri_panel["totalSessions"], 1)
        sec_list = core.list_all_sessions(workspace_filter=ws_sec)
        sec_panel = core.build_side_panel_payload(workspace_filter=ws_sec)
        self.assertEqual(sec_list, [])
        self.assertEqual(sec_panel["sessions"], [])
        self.assertEqual(sec_panel["totalSessions"], 0)

    def test_conversation_name_derivation_priority_and_legacy_fallback(self) -> None:
        """Verify derived/explicit/host title resolution, priority preservation, legacy transcript fallback, and Markdown ordering."""
        # 1. Derivation helper edge cases
        long_prompt = "整理" + ("超长标题内容" * 20)
        clipped = core.derive_conversation_name(long_prompt)
        self.assertIsNotNone(clipped)
        self.assertEqual(len(clipped), core.MAX_CONVERSATION_NAME_LEN)
        self.assertTrue(clipped.endswith("…"))

        at_mention_raw = (
            "<USER_REQUEST>@[/workspace/configs/service_rules_v1.conf.bak-20261119] "
            "检查配置</USER_REQUEST><ADDITIONAL_METADATA>ignored</ADDITIONAL_METADATA>"
        )
        self.assertEqual(
            core.derive_conversation_name(at_mention_raw),
            "@service_rules_v1.conf.bak-20261119 检查配置",
        )

        structured_task = (
            "## Recommended execution environment\n"
            "- **Surface** — Antigravity\n"
            "---\n"
            "## Task\n"
            "### Subheading skipped\n"
            "开发一个能显示「下一步指令」的插件\n"
        )
        self.assertEqual(
            core.derive_conversation_name(structured_task),
            "开发一个能显示「下一步指令」的插件",
        )

        cn_objective = "### 1. 明确目标（Objective）\n同步远端最新提交并对比新旧版本\n"
        self.assertEqual(
            core.derive_conversation_name(cn_objective),
            "同步远端最新提交并对比新旧版本",
        )

        # 2. Priority hierarchy: derived < explicit < host, and persistence across turns/cancel
        cid = "conv-title-prio-1234"
        tr_missing = str(self.art_dir / "nonexistent_tr.jsonl")
        hook.handle_pre_invocation(
            self._base_payload(
                cid,
                invocationNum=1,
                transcriptPath=tr_missing,
                lastUserInput="<USER_REQUEST>第一轮核心任务标题</USER_REQUEST>",
            )
        )
        st1 = core.load_session(cid)
        self.assertEqual(st1["firstUserInput"], "第一轮核心任务标题")
        self.assertEqual(st1["conversationName"], "第一轮核心任务标题")
        self.assertEqual(st1["conversationNameSource"], "derived")

        # Turn 2 changes lastUserInput; derived title from turn 1 must stay stable
        hook.handle_stop(self._base_payload(cid, terminationReason="NO_TOOL_CALL", fullyIdle=True))
        hook.handle_pre_invocation(
            self._base_payload(
                cid,
                invocationNum=1,
                transcriptPath=tr_missing,
                lastUserInput="第二轮补充问题",
            )
        )
        st2 = core.load_session(cid)
        self.assertEqual(st2["lastUserInput"], "第二轮补充问题")
        self.assertEqual(st2["firstUserInput"], "第一轮核心任务标题")
        self.assertEqual(st2["conversationName"], "第一轮核心任务标题")

        # Explicit conversation_name via next_step_publish upgrades source to explicit
        pub_explicit = mcp_server._handle_tool_call(
            "next_step_publish",
            {
                "conversation_id": cid,
                "next_action": "请确认显式标题",
                "conversation_name": "UI 对比与合并任务",
            },
        )
        self.assertFalse(pub_explicit["isError"])
        pub_txt = pub_explicit["content"][0]["text"]
        self.assertIn("UI 对比与合并任务", pub_txt)
        self.assertLess(pub_txt.index("UI 对比与合并任务"), pub_txt.index(cid[:8]))

        st_exp = core.load_session(cid)
        self.assertEqual(st_exp["conversationName"], "UI 对比与合并任务")
        self.assertEqual(st_exp["conversationNameSource"], "explicit")

        # Omitting conversation_name later & USER_CANCELED must preserve explicit title
        mcp_server._handle_tool_call(
            "next_step_publish",
            {"conversation_id": cid, "next_action": "继续检查"},
        )
        hook.handle_stop(self._base_payload(cid, terminationReason="USER_CANCELED", fullyIdle=True))
        hook.handle_pre_invocation(
            self._base_payload(cid, invocationNum=1, transcriptPath=tr_missing, lastUserInput="第三轮消息")
        )
        self.assertEqual(core.load_session(cid)["conversationName"], "UI 对比与合并任务")

        # Host title overrides explicit, and subsequent explicit publish cannot downgrade host
        hook.handle_pre_invocation(
            self._base_payload(
                cid,
                invocationNum=2,
                transcriptPath=tr_missing,
                conversationTitle="宿主官方会话标题",
            )
        )
        st_host = core.load_session(cid)
        self.assertEqual(st_host["conversationName"], "宿主官方会话标题")
        self.assertEqual(st_host["conversationNameSource"], "host")

        mcp_server._handle_tool_call(
            "next_step_publish",
            {
                "conversation_id": cid,
                "next_action": "再次发布",
                "conversation_name": "试图降级为显式标题",
            },
        )
        st_after = core.load_session(cid)
        self.assertEqual(st_after["conversationName"], "宿主官方会话标题")
        self.assertEqual(st_after["conversationNameSource"], "host")

        # Markdown renders conversationName before shortId
        md = core.format_session_markdown(st_after)
        self.assertLess(md.index("宿主官方会话标题"), md.index(f"#{st_after['shortId']}"))
        unbound_payload = core.build_side_panel_payload()
        unbound_txt = core.format_side_panel_text(unbound_payload)
        self.assertLess(unbound_txt.index("宿主官方会话标题"), unbound_txt.index(f"#{st_after['shortId']}"))

        # 3. Read-only legacy session fallback via transcript head (no disk mutation) + stat cache invalidation + lastUserInput fallback
        legacy_cid = "legacy-no-title-9999"
        legacy_tr = self.art_dir / "legacy_transcript.jsonl"
        legacy_tr.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>历史第一轮真实课题</USER_REQUEST>"})
            + "\n",
            encoding="utf-8",
        )
        legacy_file = core.session_path(legacy_cid)
        legacy_payload = {
            "conversationId": legacy_cid,
            "shortId": legacy_cid[:8],
            "workspacePaths": [str(self.ws_dir)],
            "transcriptPath": str(legacy_tr),
            "lastUserInput": "第五十轮随手一句",
            "lifecycle": "IDLE",
            "sourceType": "NONE",
            "waitingOnUser": False,
            "actor": "none",
            "updatedAt": time.time(),
            "updatedAtIso": core.iso_now(),
        }
        core.atomic_write_json(legacy_file, legacy_payload)
        raw_before = legacy_file.read_text(encoding="utf-8")

        panel_legacy = core.build_side_panel_payload(bound_cid=legacy_cid, binding_mode="explicit")
        self.assertEqual(panel_legacy["selectedSession"]["conversationName"], "历史第一轮真实课题")
        # Read-only panel query must not rewrite legacy JSON on disk
        self.assertEqual(legacy_file.read_text(encoding="utf-8"), raw_before)

        # Updating transcript on disk invalidates _TRANSCRIPT_NAME_CACHE cleanly
        time.sleep(0.01)
        legacy_tr.write_text(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "历史第一轮更新后课题"})
            + "\n",
            encoding="utf-8",
        )
        panel_legacy_2 = core.build_side_panel_payload(bound_cid=legacy_cid, binding_mode="explicit")
        self.assertEqual(panel_legacy_2["selectedSession"]["conversationName"], "历史第一轮更新后课题")

        # If transcript disappears, legacy state falls back to lastUserInput
        legacy_tr.unlink()
        panel_legacy_fallback = core.build_side_panel_payload(bound_cid=legacy_cid, binding_mode="explicit")
        self.assertEqual(panel_legacy_fallback["selectedSession"]["conversationName"], "第五十轮随手一句")

        # 4. Regression checks from Codex Astra audit (host>explicit across hook->MCP, failed publish preserves name, >240c CONTEXT_SUMMARY, ~missinguser expanduser)
        # 4a. Host title arriving for the first time in PreToolUse alongside explicit conversation_name must win across PreToolUse -> MCP -> PostToolUse
        cid_race = "conv-host-race-5555"
        pub_race_args = {
            "next_action": "验证宿主优先权",
            "conversation_name": "显式冲突标题",
        }
        pre_race = hook.handle_pre_tool_use(
            self._base_payload(
                cid_race,
                stepIdx=1,
                transcriptPath=tr_missing,
                lastUserInput="",
                conversationTitle="宿主发布标题",
                toolCall={"id": "tc_r1", "name": "mcp_next-step_next_step_publish", "args": pub_race_args},
            )
        )
        mcp_race_res = mcp_server._handle_tool_call(
            "next_step_publish",
            dict(pub_race_args, **pre_race["overwrite"]),
        )
        self.assertFalse(mcp_race_res["isError"])
        self.assertIn("宿主发布标题 · `#conv-hos`", mcp_race_res["content"][0]["text"])
        hook.handle_post_tool_use(
            self._base_payload(
                cid_race,
                stepIdx=1,
                transcriptPath=tr_missing,
                lastUserInput="",
                toolCall={"id": "tc_r1", "name": "mcp_next-step_next_step_publish", "args": pub_race_args},
            )
        )
        st_race = core.load_session(cid_race)
        self.assertEqual(st_race["conversationName"], "宿主发布标题")
        self.assertEqual(st_race["conversationNameSource"], "host")

        # 4b. Failed publish (PreToolUse -> PostToolUse error -> Stop ERROR) must NOT overwrite existing explicit title
        cid_fail = "conv-pub-fail-6666"
        hook.handle_pre_invocation(self._base_payload(cid_fail, invocationNum=1, transcriptPath=tr_missing, lastUserInput="初始输入"))
        mcp_server._handle_tool_call(
            "next_step_publish",
            {"conversation_id": cid_fail, "next_action": "初版", "conversation_name": "稳固原名"},
        )
        fail_args = {"next_action": "失败发布", "conversation_name": "失败的新标题"}
        hook.handle_pre_tool_use(
            self._base_payload(
                cid_fail,
                stepIdx=5,
                transcriptPath=tr_missing,
                toolCall={"id": "tc_f1", "name": "mcp_next-step_next_step_publish", "args": fail_args},
            )
        )
        hook.handle_post_tool_use(
            self._base_payload(
                cid_fail,
                stepIdx=5,
                transcriptPath=tr_missing,
                error="permission denied",
                toolCall={"id": "tc_f1", "name": "mcp_next-step_next_step_publish", "args": fail_args},
            )
        )
        hook.handle_stop(self._base_payload(cid_fail, transcriptPath=tr_missing, terminationReason="ERROR", error="permission denied"))
        st_fail = core.load_session(cid_fail)
        self.assertEqual(st_fail["conversationName"], "稳固原名")
        self.assertIsNone(st_fail["pendingDeclaration"])

        # 4c. First input consisting purely of a >240-char <CONTEXT_SUMMARY> block must not freeze garbage firstUserInput/title
        cid_ctx = "conv-ctx-summary-7777"
        long_ctx = "<CONTEXT_SUMMARY>" + ("摘要正文" * 80) + "</CONTEXT_SUMMARY>"
        hook.handle_pre_invocation(
            self._base_payload(cid_ctx, invocationNum=1, transcriptPath=tr_missing, lastUserInput=long_ctx)
        )
        st_ctx1 = core.load_session(cid_ctx)
        self.assertEqual(st_ctx1["firstUserInput"], "")
        self.assertEqual(st_ctx1["conversationName"], "")
        hook.handle_stop(self._base_payload(cid_ctx, transcriptPath=tr_missing, lastUserInput=long_ctx, terminationReason="NO_TOOL_CALL", fullyIdle=True))
        hook.handle_pre_invocation(
            self._base_payload(cid_ctx, invocationNum=1, transcriptPath=tr_missing, lastUserInput="下一轮真实任务请求")
        )
        st_ctx2 = core.load_session(cid_ctx)
        self.assertEqual(st_ctx2["firstUserInput"], "下一轮真实任务请求")
        self.assertEqual(st_ctx2["conversationName"], "下一轮真实任务请求")

        # 4d. Unresolvable ~deleteduser transcriptPath must not raise RuntimeError and must fall back cleanly to lastUserInput
        cid_tilde = "conv-tilde-user-8888"
        core.atomic_write_json(
            core.session_path(cid_tilde),
            {
                "conversationId": cid_tilde,
                "shortId": cid_tilde[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": "~nonexistent_user_xyz_999/transcript.jsonl",
                "lastUserInput": "tilde 回退标题",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        panel_tilde = core.build_side_panel_payload(bound_cid=cid_tilde, binding_mode="explicit")
        self.assertEqual(panel_tilde["selectedSession"]["conversationName"], "tilde 回退标题")

        # 5. Round 2 Astra regressions (4e..4h)
        # 5a. Explicit conversation_id when host omits conversationId in PreToolUse must not commit name on failed publish
        cid_nohost = "conv-nohost-exp-9001"
        hook.handle_pre_invocation(self._base_payload(cid_nohost, invocationNum=1, transcriptPath=tr_missing, lastUserInput="初始会话"))
        mcp_server._handle_tool_call(
            "next_step_publish",
            {"conversation_id": cid_nohost, "next_action": "已确立", "conversation_name": "稳固显式标题"},
        )
        exp_fail_args = {
            "conversation_id": cid_nohost,
            "next_action": "将被拒绝的新发布",
            "conversation_name": "不应生效的失败标题",
        }
        hook.handle_pre_tool_use(
            {
                "stepIdx": 7,
                "toolCall": {"id": "tc_nh_f", "name": "mcp_next-step_next_step_publish", "args": exp_fail_args},
            }
        )
        self.assertEqual(core.load_session(cid_nohost)["conversationName"], "稳固显式标题")
        hook.handle_post_tool_use(
            {
                "stepIdx": 7,
                "error": "permission denied",
                "toolCall": {"id": "tc_nh_f", "name": "mcp_next-step_next_step_publish", "args": exp_fail_args},
            }
        )
        self.assertEqual(core.load_session(cid_nohost)["conversationName"], "稳固显式标题")
        # Succeeding explicit-ID publish commits the new name
        hook.handle_pre_tool_use(
            {
                "stepIdx": 8,
                "toolCall": {"id": "tc_nh_ok", "name": "mcp_next-step_next_step_publish", "args": {"conversation_id": cid_nohost, "next_action": "成功发布", "conversation_name": "成功后新标题"}},
            }
        )
        hook.handle_post_tool_use(
            {
                "stepIdx": 8,
                "toolCall": {"id": "tc_nh_ok", "name": "mcp_next-step_next_step_publish", "args": {"conversation_id": cid_nohost, "next_action": "成功发布", "conversation_name": "成功后新标题"}},
            }
        )
        self.assertEqual(core.load_session(cid_nohost)["conversationName"], "成功后新标题")

        # 5b. Baseline session JSON whose lastUserInput was 240-char clipped mid <CONTEXT_SUMMARY> returns None instead of leaked tag text
        cid_old_trunc = "conv-old-trunc-9002"
        core.atomic_write_json(
            core.session_path(cid_old_trunc),
            {
                "conversationId": cid_old_trunc,
                "shortId": cid_old_trunc[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": tr_missing,
                "lastUserInput": "<CONTEXT_SUMMARY>" + ("旧截断摘要" * 44),
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        panel_old_trunc = core.build_side_panel_payload(bound_cid=cid_old_trunc, binding_mode="explicit")
        self.assertIsNone(panel_old_trunc["selectedSession"]["conversationName"])

        # 5c. Transcript with step 0 summary-only USER_EXPLICIT skips to step 1 real request even when lastUserInput is "继续"
        cid_tr_skip = "conv-tr-skip-9003"
        tr_skip = self.art_dir / "skip_summary_tr.jsonl"
        tr_skip.write_text(
            "\n".join(
                [
                    json.dumps({"step_index": 0, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<CONTEXT_SUMMARY>仅摘要</CONTEXT_SUMMARY>"}),
                    json.dumps({"step_index": 1, "type": "USER_INPUT", "source": "USER_EXPLICIT", "content": "<USER_REQUEST>真正的首轮实际课题</USER_REQUEST>"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        core.atomic_write_json(
            core.session_path(cid_tr_skip),
            {
                "conversationId": cid_tr_skip,
                "shortId": cid_tr_skip[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": str(tr_skip),
                "lastUserInput": "继续",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        panel_tr_skip = core.build_side_panel_payload(bound_cid=cid_tr_skip, binding_mode="explicit")
        self.assertEqual(panel_tr_skip["selectedSession"]["conversationName"], "真正的首轮实际课题")

        # 5d. Multi-line divider "---\n***" must derive None (never "--- ***") so next turn sets real title
        cid_div = "conv-divider-9004"
        hook.handle_pre_invocation(self._base_payload(cid_div, invocationNum=1, transcriptPath=tr_missing, lastUserInput="---\n***"))
        hook.handle_stop(self._base_payload(cid_div, transcriptPath=tr_missing, lastUserInput="", terminationReason="NO_TOOL_CALL", fullyIdle=True))
        st_div1 = core.load_session(cid_div)
        self.assertEqual(st_div1["firstUserInput"], "")
        self.assertEqual(st_div1["conversationName"], "")
        hook.handle_pre_invocation(self._base_payload(cid_div, invocationNum=1, transcriptPath=tr_missing, lastUserInput="分隔线后的真实任务"))
        st_div2 = core.load_session(cid_div)
        self.assertEqual(st_div2["firstUserInput"], "分隔线后的真实任务")
        self.assertEqual(st_div2["conversationName"], "分隔线后的真实任务")

        # 5e. Round 3 regression: literal "<CONTEXT_SUMMARY>" mention in user prompt must NOT be truncated (turns stay distinct and clear pendingDeclaration)
        cid_lit = "conv-literal-tag-9005"
        hook.handle_pre_invocation(
            self._base_payload(
                cid_lit,
                invocationNum=1,
                transcriptPath=tr_missing,
                lastUserInput="<USER_REQUEST>检查 <CONTEXT_SUMMARY> 的解析规则</USER_REQUEST>",
            )
        )
        mcp_server._handle_tool_call(
            "next_step_publish",
            {"conversation_id": cid_lit, "next_action": "旧声明应被下一轮清除"},
        )
        st_lit1 = core.load_session(cid_lit)
        self.assertEqual(st_lit1["lastUserInput"], "检查 <CONTEXT_SUMMARY> 的解析规则")
        self.assertEqual(st_lit1["conversationName"], "检查 <CONTEXT_SUMMARY> 的解析规则")
        self.assertIsNotNone(st_lit1["pendingDeclaration"])

        hook.handle_pre_invocation(
            self._base_payload(
                cid_lit,
                invocationNum=2,
                transcriptPath=tr_missing,
                lastUserInput="<USER_REQUEST>检查 <CONTEXT_SUMMARY> 的显示样式</USER_REQUEST>",
            )
        )
        st_lit2 = core.load_session(cid_lit)
        self.assertEqual(st_lit2["lastUserInput"], "检查 <CONTEXT_SUMMARY> 的显示样式")
        self.assertEqual(st_lit2["conversationName"], "检查 <CONTEXT_SUMMARY> 的解析规则")
        self.assertIsNone(st_lit2["pendingDeclaration"])

        # 5f. Round 4 regression: literal <CONTEXT_SUMMARY> inside <USER_REQUEST> alongside trailing host <CONTEXT_SUMMARY>...</CONTEXT_SUMMARY>
        cid_both = "conv-inner-outer-9006"
        hook.handle_pre_invocation(
            self._base_payload(
                cid_both,
                invocationNum=1,
                transcriptPath=tr_missing,
                lastUserInput="<USER_REQUEST>检查 <CONTEXT_SUMMARY> 的解析规则</USER_REQUEST><CONTEXT_SUMMARY>宿主摘要</CONTEXT_SUMMARY>",
            )
        )
        mcp_server._handle_tool_call(
            "next_step_publish",
            {"conversation_id": cid_both, "next_action": "第二轮应清除该声明"},
        )
        st_b1 = core.load_session(cid_both)
        self.assertEqual(st_b1["lastUserInput"], "检查 <CONTEXT_SUMMARY> 的解析规则")
        self.assertEqual(st_b1["conversationName"], "检查 <CONTEXT_SUMMARY> 的解析规则")
        self.assertIsNotNone(st_b1["pendingDeclaration"])

        hook.handle_pre_invocation(
            self._base_payload(
                cid_both,
                invocationNum=2,
                transcriptPath=tr_missing,
                lastUserInput="<USER_REQUEST>检查 <CONTEXT_SUMMARY> 的显示样式</USER_REQUEST><CONTEXT_SUMMARY>宿主摘要</CONTEXT_SUMMARY>",
            )
        )
        st_b2 = core.load_session(cid_both)
        self.assertEqual(st_b2["lastUserInput"], "检查 <CONTEXT_SUMMARY> 的显示样式")
        self.assertEqual(st_b2["conversationName"], "检查 <CONTEXT_SUMMARY> 的解析规则")
        self.assertIsNone(st_b2["pendingDeclaration"])

        # 5g. Round 5 regression: <CONTEXT_SUMMARY> enclosing an old <USER_REQUEST>...</USER_REQUEST> must skip the summary cleanly
        nested_sum_only = "<CONTEXT_SUMMARY>上一轮：<USER_REQUEST>旧请求</USER_REQUEST></CONTEXT_SUMMARY>"
        self.assertIsNone(core.derive_conversation_name(nested_sum_only))
        nested_then_cur = f"{nested_sum_only}<USER_REQUEST>当前任务</USER_REQUEST>"
        self.assertEqual(core.derive_conversation_name(nested_then_cur), "当前任务")
        cid_nest = "conv-nested-sum-9007"
        hook.handle_pre_invocation(self._base_payload(cid_nest, invocationNum=1, transcriptPath=tr_missing, lastUserInput=nested_sum_only))
        st_n1 = core.load_session(cid_nest)
        self.assertEqual(st_n1["conversationName"], "")
        self.assertEqual(st_n1["firstUserInput"], "")
        hook.handle_stop(self._base_payload(cid_nest, transcriptPath=tr_missing, lastUserInput=nested_sum_only, terminationReason="NO_TOOL_CALL", fullyIdle=True))
        hook.handle_pre_invocation(self._base_payload(cid_nest, invocationNum=1, transcriptPath=tr_missing, lastUserInput=nested_then_cur))
        st_n2 = core.load_session(cid_nest)
        self.assertEqual(st_n2["firstUserInput"], "当前任务")
        self.assertEqual(st_n2["conversationName"], "当前任务")

        # 5h. Stop-hook regression: PreToolUse for next_step_side_panel delivering a new host conversationTitle persists it
        # without resetting updatedAt / clearing TOOL_RUNNING watchdog staleness
        cid_panel_rename = "conv-panel-rename-9008"
        stale_ts = time.time() - 300.0
        core.atomic_write_json(
            core.session_path(cid_panel_rename),
            {
                "conversationId": cid_panel_rename,
                "shortId": cid_panel_rename[:8],
                "workspacePaths": [str(self.ws_dir)],
                "lastUserInput": "旧初始标题",
                "firstUserInput": "旧初始标题",
                "conversationName": "旧初始标题",
                "conversationNameSource": "derived",
                "lifecycle": "TOOL_RUNNING",
                "sourceType": "LIFECYCLE",
                "confidence": "DETERMINISTIC",
                "waitingOnUser": False,
                "actor": "agent",
                "nextAction": "正在执行工具：run_command",
                "activeTool": {"id": "tc_hang", "name": "run_command", "startedAt": stale_ts, "mayWaitApproval": True},
                "updatedAt": stale_ts,
                "updatedAtIso": core.iso_now(stale_ts),
            },
        )
        pre_sp_rename = hook.handle_pre_tool_use(
            self._base_payload(
                cid_panel_rename,
                stepIdx=3,
                transcriptPath=tr_missing,
                lastUserInput="",
                conversationTitle="宿主打开侧栏时的新名称",
                toolCall={"id": "tc_sp_rn", "name": "mcp_next-step_next_step_side_panel", "args": {}},
            )
        )
        self.assertEqual(core.load_session(cid_panel_rename)["updatedAt"], stale_ts)
        sp_rename_res = mcp_server._handle_tool_call("next_step_side_panel", pre_sp_rename["overwrite"])
        sel_rename = sp_rename_res["structuredContent"]["selectedSession"]
        self.assertEqual(sel_rename["conversationName"], "宿主打开侧栏时的新名称")
        self.assertTrue(sel_rename["stale"])
        self.assertTrue(sel_rename["copyBlocked"])
        poll_rename = mcp_server._handle_tool_call("next_step_panel_state", {"conversation_id": cid_panel_rename})["structuredContent"]["selectedSession"]
        self.assertEqual(poll_rename["conversationName"], "宿主打开侧栏时的新名称")
        self.assertTrue(poll_rename["stale"])
        self.assertTrue(poll_rename["copyBlocked"])

        # 5i. Stop-hook regression: transcript.jsonl step 0 with truncated_fields=["content"] & unclosed <CONTEXT_SUMMARY> skips to step 1 real prompt
        cid_tr_trunc = "conv-tr-trunc-9009"
        tr_trunc_path = self.art_dir / "tr_truncated_fields.jsonl"
        tr_trunc_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "step_index": 0,
                            "type": "USER_INPUT",
                            "source": "USER_EXPLICIT",
                            "truncated_fields": ["content"],
                            "content": "<CONTEXT_SUMMARY>被截断且无闭合标签的历史摘要正文",
                        }
                    ),
                    json.dumps(
                        {
                            "step_index": 1,
                            "type": "USER_INPUT",
                            "source": "USER_EXPLICIT",
                            "content": "<USER_REQUEST>订单索引迁移</USER_REQUEST>",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        # Read-only legacy resolution and PreInvocation persistence both resolve "订单索引迁移"
        core.atomic_write_json(
            core.session_path(cid_tr_trunc),
            {
                "conversationId": cid_tr_trunc,
                "shortId": cid_tr_trunc[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": str(tr_trunc_path),
                "lastUserInput": "继续",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        self.assertEqual(
            core.build_side_panel_payload(bound_cid=cid_tr_trunc, binding_mode="explicit")["selectedSession"]["conversationName"],
            "订单索引迁移",
        )
        hook.handle_pre_invocation(
            self._base_payload(
                cid_tr_trunc,
                invocationNum=1,
                transcriptPath=str(tr_trunc_path),
                lastUserInput="继续",
            )
        )
        st_trt = core.load_session(cid_tr_trunc)
        self.assertEqual(st_trt["firstUserInput"], "订单索引迁移")
        self.assertEqual(st_trt["conversationName"], "订单索引迁移")

        # 5j. Truncated nested <CONTEXT_SUMMARY> containing an old <USER_REQUEST> skips to step 1 instead of picking old request
        cid_tr_nest_trunc = "conv-tr-nest-trunc-9010"
        tr_nest_trunc = self.art_dir / "tr_nest_trunc.jsonl"
        tr_nest_trunc.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "step_index": 0,
                            "type": "USER_INPUT",
                            "source": "USER_EXPLICIT",
                            "truncated_fields": ["content"],
                            "content": "<CONTEXT_SUMMARY>历史摘要：<USER_REQUEST>旧任务</USER_REQUEST>",
                        }
                    ),
                    json.dumps(
                        {
                            "step_index": 1,
                            "type": "USER_INPUT",
                            "source": "USER_EXPLICIT",
                            "content": "<USER_REQUEST>订单索引迁移</USER_REQUEST>",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        core.atomic_write_json(
            core.session_path(cid_tr_nest_trunc),
            {
                "conversationId": cid_tr_nest_trunc,
                "shortId": cid_tr_nest_trunc[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": str(tr_nest_trunc),
                "lastUserInput": "继续",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        self.assertEqual(
            core.build_side_panel_payload(bound_cid=cid_tr_nest_trunc, binding_mode="explicit")["selectedSession"]["conversationName"],
            "订单索引迁移",
        )
        hook.handle_pre_invocation(
            self._base_payload(cid_tr_nest_trunc, invocationNum=1, transcriptPath=str(tr_nest_trunc), lastUserInput="继续")
        )
        st_nt = core.load_session(cid_tr_nest_trunc)
        self.assertEqual(st_nt["firstUserInput"], "订单索引迁移")
        self.assertEqual(st_nt["conversationName"], "订单索引迁移")

        # 5k. Truncated trailing host metadata does NOT cause double-stripping of literal <CONTEXT_SUMMARY> inside <USER_REQUEST>
        cid_tr_lit = "conv-tr-lit-trunc-9011"
        tr_lit_trunc = self.art_dir / "tr_lit_trunc.jsonl"
        tr_lit_trunc.write_text(
            json.dumps(
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "truncated_fields": ["content"],
                    "content": "<USER_REQUEST>解析 <CONTEXT_SUMMARY>保留正文</CONTEXT_SUMMARY> 的规则</USER_REQUEST><ADDITIONAL_METADATA>被截断的尾随宿主元数据",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        core.atomic_write_json(
            core.session_path(cid_tr_lit),
            {
                "conversationId": cid_tr_lit,
                "shortId": cid_tr_lit[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": str(tr_lit_trunc),
                "lastUserInput": "继续",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        expected_lit = "解析 <CONTEXT_SUMMARY>保留正文</CONTEXT_SUMMARY> 的规则"
        self.assertEqual(
            core.build_side_panel_payload(bound_cid=cid_tr_lit, binding_mode="explicit")["selectedSession"]["conversationName"],
            expected_lit,
        )
        hook.handle_pre_invocation(
            self._base_payload(cid_tr_lit, invocationNum=1, transcriptPath=str(tr_lit_trunc), lastUserInput="继续")
        )
        st_tl = core.load_session(cid_tr_lit)
        self.assertEqual(st_tl["firstUserInput"], expected_lit)
        self.assertEqual(st_tl["conversationName"], expected_lit)

        # 5l. Legacy session with lastUserInput="继续" upgrades from derived_fallback to real firstUserInput on transcript-only update
        cid_fb_up = "conv-fb-upgrade-9012"
        core.atomic_write_json(
            core.session_path(cid_fb_up),
            {
                "conversationId": cid_fb_up,
                "shortId": cid_fb_up[:8],
                "workspacePaths": [str(self.ws_dir)],
                "lastUserInput": "继续",
                "firstUserInput": "",
                "conversationName": "",
                "conversationNameSource": "",
                "transcriptPath": "",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        hook.handle_pre_invocation(
            self._base_payload(cid_fb_up, invocationNum=1, transcriptPath=tr_missing, lastUserInput="")
        )
        st_fb1 = core.load_session(cid_fb_up)
        self.assertEqual(st_fb1["conversationName"], "继续")
        self.assertEqual(st_fb1["conversationNameSource"], "derived_fallback")

        # Intermediate Hook carrying lastUserInput="继续" while transcript still unreadable must NOT promote to derived
        hook.handle_pre_invocation(
            self._base_payload(cid_fb_up, invocationNum=2, transcriptPath=tr_missing, lastUserInput="继续")
        )
        st_fb_mid = core.load_session(cid_fb_up)
        self.assertEqual(st_fb_mid["firstUserInput"], "")
        self.assertEqual(st_fb_mid["conversationName"], "继续")
        self.assertEqual(st_fb_mid["conversationNameSource"], "derived_fallback")

        tr_orders = self.art_dir / "tr_orders.jsonl"
        tr_orders.write_text(
            json.dumps(
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "source": "USER_EXPLICIT",
                    "content": "<USER_REQUEST>订单索引迁移</USER_REQUEST>",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        # Read-only panel item must resolve live transcript title when stored source is derived_fallback
        st_ro_preview = dict(st_fb_mid, transcriptPath=str(tr_orders))
        self.assertEqual(
            core.build_session_panel_item(st_ro_preview)["conversationName"],
            "订单索引迁移",
        )
        hook.handle_pre_invocation(
            self._base_payload(cid_fb_up, invocationNum=3, transcriptPath=str(tr_orders), lastUserInput="")
        )
        st_fb2 = core.load_session(cid_fb_up)
        self.assertEqual(st_fb2["firstUserInput"], "订单索引迁移")
        self.assertEqual(st_fb2["conversationName"], "订单索引迁移")
        self.assertEqual(st_fb2["conversationNameSource"], "derived")
        self.assertEqual(
            core.build_side_panel_payload(bound_cid=cid_fb_up, binding_mode="explicit")["selectedSession"]["conversationName"],
            "订单索引迁移",
        )

        # 5m. Nested <CONTEXT_SUMMARY> blocks (balanced, truncated-with-inner-USER_REQUEST, and unclosed literal+balanced)
        nested_bal = (
            "<CONTEXT_SUMMARY>外层总结提到了 <CONTEXT_SUMMARY>历史摘要</CONTEXT_SUMMARY> 历史任务</CONTEXT_SUMMARY>"
            "<USER_REQUEST>当前任务</USER_REQUEST>"
        )
        self.assertEqual(core.derive_conversation_name(nested_bal), "当前任务")
        self.assertEqual(core.sanitize_user_input(nested_bal), "当前任务")
        lit_plus_balanced = "说明 <CONTEXT_SUMMARY> 的用途，并删除示例 <CONTEXT_SUMMARY>样例</CONTEXT_SUMMARY> 后重试"
        self.assertEqual(
            core.sanitize_user_input(lit_plus_balanced),
            "说明 <CONTEXT_SUMMARY> 的用途，并删除示例 后重试",
        )
        self.assertEqual(
            core.derive_conversation_name(lit_plus_balanced),
            "说明 <CONTEXT_SUMMARY> 的用途，并删除示例 后重试",
        )

        cid_nest_bal = "conv-nest-bal-9013"
        tr_nest_bal_trunc = self.art_dir / "tr_nest_bal_trunc.jsonl"
        tr_nest_bal_trunc.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "step_index": 0,
                            "type": "USER_INPUT",
                            "source": "USER_EXPLICIT",
                            "truncated_fields": ["content"],
                            "content": (
                                "<CONTEXT_SUMMARY>外层摘要：<CONTEXT_SUMMARY>历史摘要</CONTEXT_SUMMARY>"
                                " 历史任务：<USER_REQUEST>旧请求</USER_REQUEST> 尚未闭合"
                            ),
                        }
                    ),
                    json.dumps(
                        {
                            "step_index": 1,
                            "type": "USER_INPUT",
                            "source": "USER_EXPLICIT",
                            "content": "<USER_REQUEST>当前任务</USER_REQUEST>",
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        core.atomic_write_json(
            core.session_path(cid_nest_bal),
            {
                "conversationId": cid_nest_bal,
                "shortId": cid_nest_bal[:8],
                "workspacePaths": [str(self.ws_dir)],
                "transcriptPath": str(tr_nest_bal_trunc),
                "lastUserInput": "继续",
                "lifecycle": "IDLE",
                "sourceType": "NONE",
                "waitingOnUser": False,
                "actor": "none",
                "updatedAt": time.time(),
                "updatedAtIso": core.iso_now(),
            },
        )
        self.assertEqual(
            core.build_side_panel_payload(bound_cid=cid_nest_bal, binding_mode="explicit")["selectedSession"]["conversationName"],
            "当前任务",
        )
        hook.handle_pre_invocation(
            self._base_payload(cid_nest_bal, invocationNum=1, transcriptPath=str(tr_nest_bal_trunc), lastUserInput="继续")
        )
        st_nb = core.load_session(cid_nest_bal)
        self.assertEqual(st_nb["firstUserInput"], "当前任务")
        self.assertEqual(st_nb["conversationName"], "当前任务")

    def test_antigravity_native_sidebar_titles_pbtxt_sqlite_and_side_question(self) -> None:
        """Resolve real Antigravity sidebar titles read-only from .pbtxt and SQLite WAL without side-effects."""
        import hashlib
        import sqlite3

        # 1. Direct .pbtxt parser edge cases (octal UTF-8, escapes, braces in quotes, comments, adjacent strings, nesting)
        pb_complex = """
        # Comment before header
        last_viewed: { seconds: 1700000000 title: "NestedShouldBeIgnored" }
        title: "Fix \\"quoted\\" {braces}"
        side_question_titles: {
          value: "Side Question Value First"
          key: "side-order-1"
        }
        other_msg: {
          title: "AnotherNestedTitle"
        }
        side_question_titles: { key: "side-octal" value: "Next Step " "\\345\\260\\217\\345\\267\\245\\345\\205\\267" }
        """
        parsed_title, parsed_side = core._parse_pbtxt_annotation(pb_complex)
        self.assertEqual(parsed_title, 'Fix "quoted" {braces}')
        self.assertEqual(parsed_side["side-order-1"], "Side Question Value First")
        self.assertEqual(parsed_side["side-octal"], "Next Step 小工具")

        adj_title, _ = core._parse_pbtxt_annotation(
            'title: "Application " # inline comment\n"Configuration File Checks"\n'
        )
        self.assertEqual(adj_title, "Application Configuration File Checks")

        with tempfile.TemporaryDirectory() as td:
            fake_app = Path(td).resolve() / "fake_app"
            fake_brain = fake_app / "brain"
            fake_ann = fake_app / "annotations"
            fake_ann.mkdir(parents=True, exist_ok=True)
            fake_db = fake_app / "conversation_summaries.db"

            def _make_session(
                cid: str,
                *,
                brain_subdir: bool = True,
                conv_name: str = "",
                conv_source: str = "",
                last_input: str = "继续",
                updated_offset: float = 0.0,
            ) -> None:
                art_path = (fake_brain / cid) if brain_subdir else (self.art_dir / cid)
                art_path.mkdir(parents=True, exist_ok=True)
                ts = time.time() - updated_offset
                core.atomic_write_json(
                    core.session_path(cid),
                    {
                        "conversationId": cid,
                        "shortId": cid[:8],
                        "workspacePaths": [str(self.ws_dir)],
                        "artifactDirectoryPath": str(art_path),
                        "transcriptPath": str(art_path / ".system_generated" / "logs" / "transcript.jsonl"),
                        "conversationName": conv_name,
                        "conversationNameSource": conv_source,
                        "lastUserInput": last_input,
                        "lifecycle": "IDLE",
                        "sourceType": "NONE",
                        "waitingOnUser": False,
                        "actor": "none",
                        "updatedAt": ts,
                        "updatedAtIso": core.iso_now(ts),
                    },
                )

            # 2. Live rename, priority order (host > native pbtxt > explicit/derived), & cache invalidation
            cid_live = "live-pbtxt-1111-0001"
            pb_live = fake_ann / f"{cid_live}.pbtxt"
            pb_live.write_text('title:"Antigravity Prompt Optimizer Plugin"\n', encoding="utf-8")
            _make_session(
                cid_live,
                conv_name="旧派生标题",
                conv_source="derived",
                updated_offset=10.0,
            )

            panel1 = core.build_side_panel_payload(bound_cid=cid_live, binding_mode="explicit")
            self.assertEqual(panel1["selectedSession"]["conversationName"], "Antigravity Prompt Optimizer Plugin")
            md1 = core.format_session_markdown(core.load_session(cid_live))
            self.assertIn("### Antigravity Prompt Optimizer Plugin ·", md1)

            # Explicit name persisted earlier is overridden by native Antigravity sidebar title
            st_exp = core.load_session(cid_live)
            st_exp["conversationName"] = "旧显式名称"
            st_exp["conversationNameSource"] = "explicit"
            core.save_session(cid_live, st_exp)
            panel1b = core.build_side_panel_payload(bound_cid=cid_live, binding_mode="explicit")
            self.assertEqual(panel1b["selectedSession"]["conversationName"], "Antigravity Prompt Optimizer Plugin")

            # Renaming in sidebar rewrites .pbtxt -> stat cache invalidates immediately
            old_stat = pb_live.stat()
            pb_live.write_text('title:"Next Step \\345\\260\\217\\345\\267\\245\\345\\205\\267\\346\\265\\213\\350\\257\\225\\346\\226\\271\\346\\263\\225"\n', encoding="utf-8")
            os.utime(pb_live, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 2_000_000_000))
            panel2 = core.build_side_panel_payload(bound_cid=cid_live, binding_mode="explicit")
            self.assertEqual(panel2["selectedSession"]["conversationName"], "Next Step 小工具测试方法")

            # Host hook title retains highest precedence over local .pbtxt
            st_host = core.load_session(cid_live)
            st_host["conversationName"] = "宿主实时标题"
            st_host["conversationNameSource"] = "host"
            core.save_session(cid_live, st_host)
            panel_host = core.build_side_panel_payload(bound_cid=cid_live, binding_mode="hook_bound")
            self.assertEqual(panel_host["selectedSession"]["conversationName"], "宿主实时标题")

            # 3. Separate-process SQLite WAL lifecycle (empty -> active WAL -> live rename -> PASSIVE ckpt -> TRUNCATE+close)
            #    verifying exact SHA-256 byte-for-byte immutability across .db, -wal, and -shm at every state.
            cid_parent = "parent-mvp-2222-0002"
            cid_db_only = "dbonly-codex-3333-0003"
            cid_side_q = "sideq-fork-4444-0004"
            cid_live_wal = "livewal-5555-0005"

            (fake_ann / f"{cid_parent}.pbtxt").write_text(
                f'title:"Antigravity Next Step Plugin MVP"\nside_question_titles:{{key:"{cid_side_q}" value:"UI Comparison And Fork Review"}}\n',
                encoding="utf-8",
            )
            _make_session(cid_parent, conv_name="fallback-p", conv_source="derived", updated_offset=20.0)
            _make_session(cid_db_only, conv_name="fallback-d", conv_source="derived", updated_offset=30.0)
            _make_session(cid_side_q, conv_name="fallback-s", conv_source="derived", updated_offset=40.0)
            _make_session(cid_live_wal, conv_name="fallback-w", conv_source="derived", updated_offset=5.0)

            def _snapshot_db_hashes() -> dict[str, tuple[str, int]]:
                out: dict[str, tuple[str, int]] = {}
                for fname in (
                    "conversation_summaries.db",
                    "conversation_summaries.db-wal",
                    "conversation_summaries.db-shm",
                ):
                    fp = fake_app / fname
                    if fp.exists():
                        raw = fp.read_bytes()
                        out[fname] = (hashlib.sha256(raw).hexdigest(), len(raw))
                return out

            writer_script = "\n".join(
                [
                    "import sqlite3, sys",
                    "db_path, cid_p, cid_d, cid_sq, cid_w = sys.argv[1:6]",
                    "c = sqlite3.connect(db_path)",
                    "c.execute('PRAGMA journal_mode=WAL')",
                    "c.execute('CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)')",
                    "c.execute('CREATE TABLE conversation_parents (conversation_id TEXT PRIMARY KEY, parent_id TEXT)')",
                    "c.execute('INSERT INTO conversation_summaries VALUES (?, ?)', (cid_p, 'Antigravity Next Step Plugin MVP'))",
                    "c.commit()",
                    "c.execute('PRAGMA wal_checkpoint(TRUNCATE)')",
                    "print('READY_V0', flush=True)",
                    "for line in sys.stdin:",
                    "    cmd = line.strip()",
                    "    if cmd == 'v1':",
                    "        c.execute('INSERT INTO conversation_summaries VALUES (?, ?)', (cid_d, 'Configure Codex Audit Model'))",
                    "        c.execute('INSERT INTO conversation_parents VALUES (?, ?)', (cid_sq, cid_p))",
                    "        c.commit()",
                    "        print('READY_V1', flush=True)",
                    "    elif cmd == 'v2':",
                    "        c.execute('UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?', ('Configure Codex Audit Model v2', cid_d))",
                    "        c.execute('INSERT INTO conversation_summaries VALUES (?, ?)', (cid_w, 'Application Configuration File Checks'))",
                    "        c.commit()",
                    "        print('READY_V2', flush=True)",
                    "    elif cmd == 'passive_v3':",
                    "        c.execute('PRAGMA wal_checkpoint(PASSIVE)')",
                    "        c.execute('UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?', ('Configure Codex Audit Model v3', cid_d))",
                    "        c.commit()",
                    "        print('READY_V3', flush=True)",
                    "    elif cmd == 'truncate_close':",
                    "        c.execute('PRAGMA wal_checkpoint(TRUNCATE)')",
                    "        c.close()",
                    "        print('CLOSED', flush=True)",
                    "        break",
                ]
            )

            proc = subprocess.Popen(
                [sys.executable, "-c", writer_script, str(fake_db), cid_parent, cid_db_only, cid_side_q, cid_live_wal],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            wal_file = Path(f"{fake_db}-wal")
            shm_file = Path(f"{fake_db}-shm")
            try:
                # V0: Empty/zeroed WAL after TRUNCATE checkpoint while writer holds DB open
                self.assertEqual(proc.stdout.readline().strip(), "READY_V0")
                h0_before = _snapshot_db_hashes()
                p0 = core.build_side_panel_payload()
                by0 = {item["conversationId"]: item["conversationName"] for item in p0["sessions"]}
                self.assertEqual(by0[cid_parent], "Antigravity Next Step Plugin MVP")
                self.assertEqual(by0[cid_db_only], "fallback-d")
                self.assertEqual(_snapshot_db_hashes(), h0_before)

                # V1: Active WAL commit (reader must see committed WAL rows & parent side-question, zero -shm write!)
                proc.stdin.write("v1\n")
                proc.stdin.flush()
                self.assertEqual(proc.stdout.readline().strip(), "READY_V1")
                self.assertTrue(wal_file.is_file() and wal_file.stat().st_size > 0)
                self.assertTrue(shm_file.is_file())
                h1_before = _snapshot_db_hashes()
                p1 = core.build_side_panel_payload()
                by1 = {item["conversationId"]: item["conversationName"] for item in p1["sessions"]}
                self.assertEqual(by1[cid_db_only], "Configure Codex Audit Model")
                self.assertEqual(by1[cid_side_q], "UI Comparison And Fork Review")
                self.assertEqual(_snapshot_db_hashes(), h1_before)

                # V2: Live rename & insert while writer holds connection open -> immediate visibility & zero byte drift
                proc.stdin.write("v2\n")
                proc.stdin.flush()
                self.assertEqual(proc.stdout.readline().strip(), "READY_V2")
                h2_before = _snapshot_db_hashes()
                p2 = core.build_side_panel_payload()
                by2 = {item["conversationId"]: item["conversationName"] for item in p2["sessions"]}
                self.assertEqual(by2[cid_db_only], "Configure Codex Audit Model v2")
                self.assertEqual(by2[cid_live_wal], "Application Configuration File Checks")
                self.assertEqual(_snapshot_db_hashes(), h2_before)

                # V3: PASSIVE checkpoint + follow-up rename -> cache invalidates cleanly across non-truncating checkpoint
                proc.stdin.write("passive_v3\n")
                proc.stdin.flush()
                self.assertEqual(proc.stdout.readline().strip(), "READY_V3")
                h3_before = _snapshot_db_hashes()
                p3 = core.build_side_panel_payload()
                by3 = {item["conversationId"]: item["conversationName"] for item in p3["sessions"]}
                self.assertEqual(by3[cid_db_only], "Configure Codex Audit Model v3")
                self.assertEqual(_snapshot_db_hashes(), h3_before)

                # V4: TRUNCATE checkpoint + close -> zero -wal/-shm sidecars exist or get created, SHA-256 unchanged
                proc.stdin.write("truncate_close\n")
                proc.stdin.flush()
                self.assertEqual(proc.stdout.readline().strip(), "CLOSED")
            finally:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
                proc.wait(timeout=5)

            self.assertFalse(wal_file.exists())
            self.assertFalse(shm_file.exists())
            before_dir_entries = sorted(p.name for p in fake_app.iterdir())
            h4_before = _snapshot_db_hashes()
            p4 = core.build_side_panel_payload()
            by4 = {item["conversationId"]: item["conversationName"] for item in p4["sessions"]}
            self.assertEqual(by4[cid_db_only], "Configure Codex Audit Model v3")
            self.assertEqual(_snapshot_db_hashes(), h4_before)
            self.assertEqual(sorted(p.name for p in fake_app.iterdir()), before_dir_entries)

            # 3b. Unstable states: hot rollback -journal, mismatched WalIndexHdr twin copies, & mid-copy race retry/exhaustion
            race_app = Path(td).resolve() / "race_app"
            (race_app / "brain" / "r1").mkdir(parents=True, exist_ok=True)
            race_db = race_app / "conversation_summaries.db"
            rc = sqlite3.connect(str(race_db))
            rc.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
            rc.execute("INSERT INTO conversation_summaries VALUES ('race-cid', 'Stable Race Title')")
            rc.commit()
            rc.close()

            # Non-empty -journal pauses DB read cleanly without caching failure
            hot_journal = Path(f"{race_db}-journal")
            hot_journal.write_bytes(b"active-hot-rollback-journal")
            race_state = {
                "conversationId": "race-cid",
                "artifactDirectoryPath": str(race_app / "brain" / "r1"),
                "conversationName": "回退标题",
                "conversationNameSource": "derived",
            }
            self.assertEqual(core._load_summaries_db_maps(race_db), ({}, {}))
            self.assertEqual(core.resolve_session_conversation_name(race_state), "回退标题")
            hot_journal.unlink()
            self.assertEqual(core.resolve_session_conversation_name(race_state), "Stable Race Title")

            # Mismatched WalIndexHdr twin copies in -shm report UNSTABLE and skip reading torn WAL
            bad_shm = Path(f"{race_db}-shm")
            bad_shm.write_bytes(b"A" * 48 + b"B" * 48 + b"\x00" * 4)
            self.assertEqual(core._snapshot_db_signature(race_db), ("UNSTABLE", None, 0))
            with mock.patch.object(core.time, "sleep"):
                self.assertEqual(core._load_summaries_db_maps(race_db), ({}, {}))
            bad_shm.unlink()

            # Clear DB cache cleanly and verify mid-copy race retry recovery & exhaustion degradation
            core._SUMMARIES_DB_CACHE.clear()
            st_race = race_db.stat()
            sig_a = (str(race_db), (st_race.st_mtime_ns, st_race.st_size, st_race.st_ino), b"A" * 100, None, b"", None, None)
            sig_b = (str(race_db), (st_race.st_mtime_ns + 1, st_race.st_size, st_race.st_ino), b"B" * 100, None, b"", None, None)
            sig_c = (str(race_db), (st_race.st_mtime_ns + 2, st_race.st_size, st_race.st_ino), b"C" * 100, None, b"", None, None)

            with (
                mock.patch.object(core.time, "sleep"),
                mock.patch.object(
                    core,
                    "_snapshot_db_signature",
                    side_effect=[
                        ("OK", sig_a, st_race.st_size),
                        ("OK", sig_b, st_race.st_size),
                        ("OK", sig_c, st_race.st_size),
                        ("OK", sig_c, st_race.st_size),
                    ],
                ),
            ):
                tmap_retry, _ = core._load_summaries_db_maps(race_db)
                self.assertEqual(tmap_retry.get("race-cid"), "Stable Race Title")

            core._SUMMARIES_DB_CACHE.clear()
            with (
                mock.patch.object(core.time, "sleep"),
                mock.patch.object(
                    core,
                    "_snapshot_db_signature",
                    side_effect=[
                        ("OK", sig_a, st_race.st_size),
                        ("OK", sig_b, st_race.st_size),
                        ("OK", sig_a, st_race.st_size),
                        ("OK", sig_b, st_race.st_size),
                        ("OK", sig_a, st_race.st_size),
                        ("OK", sig_b, st_race.st_size),
                    ],
                ),
            ):
                self.assertEqual(core._load_summaries_db_maps(race_db), ({}, {}))
            # Subsequent unmocked poll still succeeds because failed retries were not cached
            self.assertEqual(core.resolve_session_conversation_name(race_state), "Stable Race Title")

            # Disk-full TemporaryDirectory OSError falls back cleanly without failing next_step_panel_state
            cid_diskfull = "diskfull-fallback-7777"
            _make_session(cid_diskfull, conv_name="订单迁移", conv_source="explicit", updated_offset=2.0)
            core._SUMMARIES_DB_CACHE.clear()
            with (
                mock.patch.object(core.time, "sleep"),
                mock.patch.object(
                    core.tempfile,
                    "TemporaryDirectory",
                    side_effect=OSError(28, "No space left on device"),
                ),
            ):
                df_res = mcp_server._handle_tool_call("next_step_panel_state", {"conversation_id": cid_diskfull})
                self.assertFalse(df_res.get("isError", False))
                self.assertEqual(df_res["structuredContent"]["selectedSession"]["conversationName"], "订单迁移")

            # Source DB growing beyond MAX_SUMMARIES_DB_BYTES mid-copy aborts at recorded cap without exceeding cap
            core._SUMMARIES_DB_CACHE.clear()
            small_cap = 2048
            self.assertGreater(race_db.stat().st_size, small_cap)
            sig_grown = (str(race_db), (st_race.st_mtime_ns, small_cap, st_race.st_ino), b"G" * 100, None, b"", None, None)
            written_sizes: list[int] = []
            real_copy_capped = core._copy_exact_cap

            def _spy_copy_capped(src: object, dst: object, cap_bytes: int, chunk_size: int = 1024 * 1024) -> bool:
                ok = real_copy_capped(src, dst, cap_bytes, chunk_size)
                written_sizes.append(dst.tell())
                return ok

            with (
                mock.patch.object(core, "MAX_SUMMARIES_DB_BYTES", small_cap),
                mock.patch.object(core.time, "sleep"),
                mock.patch.object(core, "_copy_exact_cap", side_effect=_spy_copy_capped),
                mock.patch.object(
                    core,
                    "_snapshot_db_signature",
                    return_value=("OK", sig_grown, small_cap),
                ),
            ):
                self.assertEqual(core._load_summaries_db_maps(race_db), ({}, {}))
            self.assertEqual(len(written_sizes), core.MAX_DB_SNAPSHOT_RETRIES)
            self.assertTrue(all(sz <= small_cap for sz in written_sizes))
            self.assertEqual(core.resolve_session_conversation_name(race_state), "Stable Race Title")

            # 4. plugin_data sibling fallback + strict isolation + graceful degradation on bad files & traversal
            nested_plugin_data = fake_app / "plugin_data" / "next-step"
            nested_plugin_data.mkdir(parents=True, exist_ok=True)
            try:
                core.set_data_dir(nested_plugin_data)
                cid_no_paths = "no-paths-pbtxt-6666"
                (fake_ann / f"{cid_no_paths}.pbtxt").write_text('title:"Sibling PluginData Title"\n', encoding="utf-8")
                no_path_state = {
                    "conversationId": cid_no_paths,
                    "shortId": cid_no_paths[:8],
                    "workspacePaths": [str(self.ws_dir)],
                    "conversationName": "",
                    "conversationNameSource": "",
                    "lastUserInput": "继续",
                    "lifecycle": "IDLE",
                    "sourceType": "NONE",
                    "waitingOnUser": False,
                    "actor": "none",
                    "updatedAt": time.time(),
                    "updatedAtIso": core.iso_now(),
                }
                self.assertEqual(core.resolve_antigravity_native_title(no_path_state), "Sibling PluginData Title")
                self.assertEqual(core.resolve_session_conversation_name(no_path_state), "Sibling PluginData Title")
            finally:
                core.set_data_dir(self.data_dir)

            # Corrupt DB, schema drift without conversation_parents, malformed .pbtxt, & traversal cid all degrade silently
            bad_app = Path(td).resolve() / "bad_app"
            (bad_app / "annotations").mkdir(parents=True, exist_ok=True)
            (bad_app / "brain" / "sess").mkdir(parents=True, exist_ok=True)
            (bad_app / "conversation_summaries.db").write_bytes(b"not a valid sqlite database file")
            (bad_app / "annotations" / "bad-pb.pbtxt").write_text('title: "unclosed\nside_question_titles: { key: "x', encoding="utf-8")
            bad_state = {
                "conversationId": "bad-pb",
                "shortId": "bad-pb",
                "artifactDirectoryPath": str(bad_app / "brain" / "sess"),
                "conversationName": "保留显式回退",
                "conversationNameSource": "explicit",
            }
            self.assertIsNone(core.resolve_antigravity_native_title(bad_state))
            self.assertEqual(core.resolve_session_conversation_name(bad_state), "保留显式回退")

            traversal_state = {
                "conversationId": "../escape",
                "artifactDirectoryPath": str(fake_brain / "sess"),
            }
            self.assertIsNone(core.resolve_antigravity_native_title(traversal_state))

            # Schema drift: DB without conversation_parents table still resolves summaries and skips parents cleanly
            drift_app = Path(td).resolve() / "drift_app"
            (drift_app / "brain" / "s1").mkdir(parents=True, exist_ok=True)
            drift_db = drift_app / "conversation_summaries.db"
            dc = sqlite3.connect(str(drift_db))
            dc.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, title TEXT)")
            dc.execute("INSERT INTO conversation_summaries VALUES ('drift-sum', 'Drift Table Summary Title')")
            dc.commit()
            dc.close()
            self.assertEqual(
                core.resolve_antigravity_native_title(
                    {"conversationId": "drift-sum", "artifactDirectoryPath": str(drift_app / "brain" / "s1")}
                ),
                "Drift Table Summary Title",
            )
            self.assertIsNone(
                core.resolve_antigravity_native_title(
                    {"conversationId": "drift-miss", "artifactDirectoryPath": str(drift_app / "brain" / "s1")}
                )
            )

            # Isolation invariant: non-'brain' test artifact path with real host UUID NEVER touches host ~/.gemini/antigravity/
            isolated_host_uuid_state = {
                "conversationId": "a7bea7d3-0c0d-4be2-ad3b-34fe546c1a23",
                "artifactDirectoryPath": str(self.art_dir),
                "transcriptPath": str(self.art_dir / "transcript.jsonl"),
                "firstUserInput": "测试隔离名称",
            }
            self.assertIsNone(core.resolve_antigravity_native_title(isolated_host_uuid_state))
            self.assertEqual(core.resolve_session_conversation_name(isolated_host_uuid_state), "测试隔离名称")


if __name__ == "__main__":
    unittest.main()
