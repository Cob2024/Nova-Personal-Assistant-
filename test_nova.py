import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import assistant
import nova_calendar
import nova_memory
import nova_skills
import nova_voice


class NovaTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = nova_skills.CONFIG_FILE
        self._orig_rem = nova_skills.REM_FILE
        self._orig_notes = nova_skills.NOTES
        self._orig_routines = nova_skills.ROUTINES_FILE
        self._orig_events = nova_calendar.EVENTS_FILE
        self._orig_cal_dir = nova_calendar.CAL_DIR
        self._orig_cal_cfg = nova_calendar.CONFIG_FILE
        self._orig_tasks = assistant.TASKS_FILE
        self._orig_mem = nova_skills._mem
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        nova_skills.CONFIG_FILE = tmp_path / "config.json"
        nova_skills.REM_FILE = tmp_path / "reminders.json"
        nova_skills.NOTES = tmp_path / "Nova Notes.txt"
        nova_skills.ROUTINES_FILE = tmp_path / "routines.json"
        nova_calendar.EVENTS_FILE = tmp_path / "events.json"
        nova_calendar.CAL_DIR = tmp_path / "calendars"
        nova_calendar.CONFIG_FILE = nova_skills.CONFIG_FILE
        nova_calendar._alerted.clear()
        assistant.TASKS_FILE = tmp_path / "tasks.json"
        nova_skills._mem = nova_memory.Memory(tmp_path / "memory.db")
        nova_skills.reset_ai()
        nova_skills.reset_ai_cooldown()
        assistant.setup_skills()

    def tearDown(self):
        nova_skills.CONFIG_FILE = self._orig_cfg
        nova_skills.REM_FILE = self._orig_rem
        nova_skills.NOTES = self._orig_notes
        nova_skills.ROUTINES_FILE = self._orig_routines
        nova_calendar.EVENTS_FILE = self._orig_events
        nova_calendar.CAL_DIR = self._orig_cal_dir
        nova_calendar.CONFIG_FILE = self._orig_cal_cfg
        nova_calendar._alerted.clear()
        assistant.TASKS_FILE = self._orig_tasks
        nova_skills._mem = self._orig_mem
        nova_skills.reset_ai()
        nova_skills.reset_ai_cooldown()
        self.tmp.cleanup()

    def test_backwards_compat_anthropic_config(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({"api_key": "sk-test"}))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(nova_skills.ai_status(), (True, "anthropic", "claude-haiku-5-5"))

    def test_ollama_cloud_env_and_defaults(self):
        with mock.patch.dict("os.environ", {"OLLAMA_API_KEY": "ol-key"}, clear=True):
            self.assertEqual(nova_skills.ai_status(), (True, "ollama_cloud", "deepseek-v4.1-flash"))
            self.assertEqual(nova_skills._ai_settings()[3], "https://ollama.com/v1")

    def test_dotenv_loader_and_precedence(self):
        env_file = Path(self.tmp.name) / ".env"
        env_file.write_bytes(b'\xef\xbb\xbf# comment\nOLLAMA_API_KEY="from-dotenv"\nOPENAI_API_KEY=\'from-dotenv-oa\'\n')
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "real-env-wins"}, clear=True):
            nova_skills._load_dotenv(env_file)
            self.assertEqual(os.environ.get("OLLAMA_API_KEY"), "from-dotenv")
            self.assertEqual(os.environ.get("OPENAI_API_KEY"), "real-env-wins")

    def test_ollama_ready_without_api_key(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({"provider": "ollama", "model": "qwen2.5:7b"}))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(nova_skills.ai_status(), (True, "ollama", "qwen2.5:7b"))

    def test_env_provider_detection(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": "gem-key"}, clear=True):
            self.assertEqual(nova_skills.ai_status(), (True, "gemini", "gemini-2.5-flash"))
        with mock.patch.dict("os.environ", {"OPENAI_API_KEY": "oa-key"}, clear=True):
            self.assertEqual(nova_skills.ai_status(), (True, "openai", "gpt-4o-mini"))

    def test_new_tools_execution(self):
        nova_skills.take_note("Buy coffee beans")
        self.assertIn("Buy coffee beans", nova_skills._run_tool("read_notes", {"count": 1}))
        nova_skills.set_timer(120)
        self.assertIn("timer", nova_skills._run_tool("list_reminders", {"kind": "all"}).lower())
        self.assertIn("Cancelled 1", nova_skills._run_tool("cancel_reminders", {"kind": "timer"}))

    def test_ai_error_messages(self):
        self.assertIn("credits", nova_skills._ai_error_reply("ollama_cloud", RuntimeError("HTTP 402: Add usage credits")).lower())
        self.assertIn("rejected", nova_skills._ai_error_reply("openai", RuntimeError("HTTP 401: Unauthorized")).lower())
        self.assertIn("rate limited", nova_skills._ai_error_reply("gemini", RuntimeError("HTTP 429: Too Many Requests")).lower())

    def test_ollama_cloud_chat_and_tool_fallback(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "ollama_cloud", "api_key": "ol-key", "model": "deepseek-v4.1-flash"
        }))
        responses = [
            RuntimeError("HTTP 400: model does not support tools"),
            {"choices": [{"message": {"role": "assistant", "content": "Hello from DeepSeek on Ollama Cloud."}}]},
        ]
        with mock.patch.object(nova_skills, "_http_json", side_effect=responses) as m:
            reply = nova_skills.ask_ai("Hello!")
        self.assertEqual(reply, "Hello from DeepSeek on Ollama Cloud.")
        self.assertEqual(m.call_args_list[0].args[0], "https://ollama.com/v1/chat/completions")
        self.assertIn("tools", m.call_args_list[0].args[1])
        self.assertNotIn("tools", m.call_args_list[1].args[1])

    def test_openai_tool_loop(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "openai", "api_key": "oa-key", "model": "gpt-4o-mini"
        }))
        nova_skills.take_note("Project Jarvis phase 1")
        responses = [
            {"choices": [{"message": {
                "role": "assistant",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "read_notes", "arguments": '{"count": 1}'}}]
            }}]},
            {"choices": [{"message": {"role": "assistant", "content": "Your latest note says Project Jarvis phase 1."}}]},
        ]
        with mock.patch.object(nova_skills, "_http_json", side_effect=responses) as m:
            reply = nova_skills.ask_ai("Read my latest note")
        self.assertEqual(reply, "Your latest note says Project Jarvis phase 1.")
        self.assertEqual(m.call_count, 2)
        self.assertEqual(len(nova_skills._history), 2)

    def test_gemini_tool_loop(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "gemini", "api_key": "gem-key", "model": "gemini-2.5-flash"
        }))
        responses = [
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "add_task", "args": {"text": "test gemini"}}}
            ]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [
                {"text": "Added test gemini to your list."}
            ]}}]},
        ]
        with mock.patch.object(nova_skills, "_http_json", side_effect=responses), \
             mock.patch.dict(nova_skills.ctx, {"add_task": lambda text: f"Added {text}"}):
            reply = nova_skills.ask_ai("Add a task to test gemini")
        self.assertEqual(reply, "Added test gemini to your list.")
        decls = nova_skills._gemini_tools()[0]["functionDeclarations"]
        list_tasks_decl = next(d for d in decls if d["name"] == "list_tasks")
        self.assertNotIn("parameters", list_tasks_decl)

    def test_ollama_tools_unsupported_fallback(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "ollama", "model": "tinyllama"
        }))
        responses = [
            RuntimeError("HTTP 400: tinyllama does not support tools"),
            {"message": {"role": "assistant", "content": "Hello from local Ollama."}},
        ]
        with mock.patch.object(nova_skills, "_http_json", side_effect=responses) as m:
            reply = nova_skills.ask_ai("Say hi")
        self.assertEqual(reply, "Hello from local Ollama.")
        self.assertIn("tools", m.call_args_list[0].args[1])
        self.assertNotIn("tools", m.call_args_list[1].args[1])

    def test_anthropic_rest_tool_loop(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "anthropic", "api_key": "ant-key", "model": "claude-haiku-5-5"
        }))
        responses = [
            {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "tu1", "name": "take_note", "input": {"text": "test note"}}
            ]},
            {"stop_reason": "end_turn", "content": [
                {"type": "text", "text": "Saved that note."}
            ]},
        ]
        real_import = __import__

        def block_anthropic(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no anthropic")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=block_anthropic), \
             mock.patch.object(nova_skills, "_http_json", side_effect=responses):
            reply = nova_skills.ask_ai("Remember test note")
        self.assertEqual(reply, "Saved that note.")

    def test_memory_persistence_and_voice_commands(self):
        r1 = assistant.handle("remember that my favorite coffee is espresso")
        self.assertIn("espresso", r1.lower())
        r2 = assistant.handle("what do you remember about coffee")
        self.assertIn("espresso", r2.lower())
        # Ensure "remember to ..." still creates a task rather than a memory fact
        r3 = assistant.handle("remember to call the dentist")
        self.assertIn("added call the dentist", r3.lower())
        # Cross-session episode survives reset_ai()
        nova_skills._mem.add_episode("How's the weather?", "It is sunny.")
        nova_skills.reset_ai()
        sys_prompt = nova_skills._system()
        self.assertIn("espresso", sys_prompt)
        self.assertIn("It is sunny.", sys_prompt)
        r4 = assistant.handle("forget favorite coffee")
        self.assertIn("forgot 1", r4.lower())

    def test_active_window_and_vision(self):
        if os.name == "nt":
            win = nova_skills.active_window()
            self.assertTrue(win is None or isinstance(win, dict))
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "ollama_cloud", "api_key": "ol-key", "model": "deepseek-v4.1-flash"
        }))
        with mock.patch.object(nova_skills, "capture_screen_b64", return_value=("QUJD", "image/jpeg")), \
             mock.patch.object(nova_skills, "_http_json", return_value={
                 "choices": [{"message": {"content": "You have a Python editor open."}}]
             }) as m:
            reply = assistant.handle("what's on my screen right now")
        self.assertEqual(reply, "You have a Python editor open.")
        sent_msg = m.call_args.args[1]["messages"][1]["content"]
        self.assertEqual(sent_msg[1]["image_url"]["url"], "data:image/jpeg;base64,QUJD")

    def test_voice_interrupt_and_sentence_split(self):
        chunks = nova_voice._split_sentences(
            "First sentence is quite long enough to split properly here. "
            "Second sentence continues with even more detail for Jarvis mode."
        )
        self.assertEqual(len(chunks), 2)
        v = nova_voice.Voice(engine="sapi")
        spoken = []

        def fake_win(chunk):
            spoken.append(chunk)
            v.stop()

        with mock.patch.object(v, "_windows", side_effect=fake_win):
            used = v.say("First sentence is fairly long so it splits cleanly. Second sentence should be skipped.")
        self.assertEqual(used, "sapi")
        self.assertEqual(len(spoken), 1)
        self.assertNotIn("sapi", v.failed)

    def test_hotkey_parser_and_mic_wake_event(self):
        import numpy as np
        self.assertEqual(assistant._parse_hotkey("ctrl+shift+space"), (0x4006, 0x20))
        self.assertIsNone(assistant._parse_hotkey(""))

        mic = assistant.Mic.__new__(assistant.Mic)
        mic.np = np
        mic.sr = 16000
        mic.ambient = 0.001
        mic.model = mock.Mock()
        mic.voice_lock = None
        wake = threading.Event()
        wake.set()

        class DummyStream:
            read_available = 0
            def read(self, n):
                raise AssertionError("stream.read should not be called once wake_event is set")

        mic.stream = DummyStream()
        self.assertEqual(mic.listen(wait=5, wake_event=wake), "")

    def test_handle_long_ai_utterance(self):
        long_prompt = " ".join(["explain"] * 55)
        with mock.patch.object(nova_skills, "ask_ai", return_value="Long answer.") as m:
            reply = assistant.handle(long_prompt)
        self.assertEqual(reply, "Long answer.")
        m.assert_called_once()

    def test_say_uses_nova_voice_and_tracks_speaking(self):
        seen_speaking = []

        def fake_say(text):
            seen_speaking.append(assistant._speaking.is_set())
            return "sapi"

        fake_voice = mock.Mock()
        fake_voice.say.side_effect = fake_say
        with mock.patch.object(assistant, "_get_voice", return_value=fake_voice):
            used = assistant.say("Testing voice integration")
        self.assertEqual(used, "sapi")
        self.assertEqual(seen_speaking, [True])
        self.assertFalse(assistant._speaking.is_set())

    def test_mic_voice_lock_rejection_and_acceptance(self):
        import numpy as np
        mic = assistant.Mic.__new__(assistant.Mic)
        mic.np = np
        mic.sr = 16000
        mic.ambient = 0.001
        seg = SimpleNamespace(text="open notepad", no_speech_prob=0.1, avg_logprob=-0.2)
        mic.model = mock.Mock()
        mic.model.transcribe.return_value = ([seg], None)
        mic.voice_lock = mock.Mock(ready=True, threshold=0.5)

        loud_chunk = np.full((1600, 1), 0.1, dtype=np.float32)
        quiet_chunk = np.zeros((1600, 1), dtype=np.float32)

        class FakeStream:
            read_available = 0
            def __init__(self, items):
                self.items = list(items)
            def read(self, n):
                return self.items.pop(0)

        mic.voice_lock.score.return_value = 0.25
        mic.stream = FakeStream([(loud_chunk, False)] * 6 + [(quiet_chunk, False)] * 12)
        self.assertEqual(mic.listen(wait=5), "")
        mic.model.transcribe.assert_not_called()

        mic.voice_lock.score.return_value = 0.78
        mic.stream = FakeStream([(loud_chunk, False)] * 6 + [(quiet_chunk, False)] * 12)
        self.assertEqual(mic.listen(wait=5), "open notepad")
        mic.model.transcribe.assert_called_once()

    def test_sentence_emitter_and_streaming_ai(self):
        emitted = []
        em = nova_skills._SentenceEmitter(emitted.append)
        for tok in ["1. First check ", "is running smoothly right now. ", "Second sentence ", "finishes cleanly here."]:
            em.feed(tok)
        full = em.flush()
        self.assertEqual(emitted, [
            "1. First check is running smoothly right now.",
            "Second sentence finishes cleanly here.",
        ])
        self.assertIn("First check", full)

        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "ollama_cloud", "api_key": "ol-key", "model": "deepseek-v4.1-flash"
        }))
        nova_skills.take_note("Streaming test note")
        stream_turns = [
            [
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"t1","function":{"name":"read_notes","arguments":"{\\"count\\": "}}]}}]}',
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"1}"}}]}}]}',
                'data: [DONE]',
            ],
            [
                'data: {"choices":[{"delta":{"content":"I checked your saved notes. "}}]}',
                'data: {"choices":[{"delta":{"content":"Your latest note says Streaming test note."}}]}',
                'data: [DONE]',
            ],
        ]
        spoken = []
        with mock.patch.object(nova_skills, "_http_stream", side_effect=[iter(t) for t in stream_turns]), \
             mock.patch.object(assistant, "say", side_effect=spoken.append):
            ended = assistant.respond("check my notes", stream=True)
        self.assertFalse(ended)
        self.assertEqual(spoken, [
            "I checked your saved notes.",
            "Your latest note says Streaming test note.",
        ])

    def test_anthropic_and_ollama_streaming(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "anthropic", "api_key": "ant-key", "model": "claude-haiku-5-5"
        }))
        ant_lines = [
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Systems are completely nominal today. "}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Ready when you are."}}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}',
        ]
        out = []
        with mock.patch.object(nova_skills, "_http_stream", return_value=iter(ant_lines)):
            reply = nova_skills.ask_ai("status report", on_sentence=out.append)
        self.assertEqual(out, ["Systems are completely nominal today.", "Ready when you are."])
        self.assertIn("Systems are completely nominal", reply)

        nova_skills.CONFIG_FILE.write_text(json.dumps({"provider": "ollama", "model": "llama3.2"}))
        ol_lines = [
            json.dumps({"message": {"role": "assistant", "content": "Local stream is working nicely today. "}}),
            json.dumps({"message": {"role": "assistant", "content": "No cloud required."}}),
        ]
        out2 = []
        with mock.patch.object(nova_skills, "_http_stream", return_value=iter(ol_lines)):
            reply2 = nova_skills.ask_ai("local check", on_sentence=out2.append)
        self.assertEqual(out2, ["Local stream is working nicely today.", "No cloud required."])
        self.assertIn("No cloud required.", reply2)

    def test_core_audio_and_diagnostics(self):
        with mock.patch.object(nova_skills, "_audio_cmd", return_value=(42, False)) as m:
            self.assertEqual(nova_skills.volume_set(42), "Volume set to 42 percent.")
            m.assert_called_with("set", 42)
        with mock.patch.object(nova_skills, "_audio_cmd", return_value=(52, False)):
            self.assertEqual(nova_skills.volume_change("up", 10), "Volume at 52 percent.")
        with mock.patch.object(nova_skills, "_audio_cmd", return_value=(52, True)):
            self.assertEqual(nova_skills.route("mute", "mute"), "Muted.")
        with mock.patch.object(nova_skills, "_audio_cmd", return_value=None), \
             mock.patch.object(nova_skills, "press") as p:
            nova_skills.volume_mute(True)
            p.assert_called_once_with("mute")
        diag = assistant.handle("system diagnostics")
        self.assertIn("gigabytes free", diag.lower())
        if os.name == "nt":
            self.assertIn("memory is at", diag.lower())
            self.assertIn("uptime is", diag.lower())

    def test_recurring_reminders_and_low_battery_guard(self):
        r1 = assistant.handle("remind me every 30 minutes to drink water")
        self.assertIn("every 30 minutes", r1.lower())
        items = nova_skills._load_rem()
        self.assertEqual(items[0]["repeat_secs"], 1800)
        self.assertIn("every 30 minutes", nova_skills.list_reminders().lower())

        r2 = assistant.handle("remind me every day at 9 am to stretch")
        self.assertIn("every day at", r2.lower())
        items = nova_skills._load_rem()
        self.assertEqual(items[1]["repeat_secs"], 86400)

        alerts = []
        nova_skills._battery_warned = False
        with mock.patch.dict(nova_skills.ctx, {"alert": lambda k, txt: alerts.append((k, txt))}), \
             mock.patch.object(nova_skills, "_battery_info", return_value=(12, False, 1200)):
            nova_skills._check_proactive_battery()
            nova_skills._check_proactive_battery()     # must not spam
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "battery")
        with mock.patch.object(nova_skills, "_battery_info", return_value=(50, True, 0)):
            nova_skills._check_proactive_battery()
        self.assertFalse(nova_skills._battery_warned)

    def test_localhost_ipc_ask_wake_stop(self):
        orig_port = assistant.IPC_PORT
        orig_lock = assistant._lock
        assistant.IPC_PORT = 47699
        try:
            self.assertFalse(assistant.already_running())
            res = assistant.send_ipc({"op": "ask", "text": "what time is it", "speak": False})
            self.assertTrue(res and res["ok"])
            self.assertIn("It's", res["reply"])

            assistant._wake_event.clear()
            res2 = assistant.send_ipc({"op": "wake"})
            self.assertTrue(res2 and res2["ok"])
            self.assertTrue(assistant._wake_event.is_set())

            assistant._stop_speech_event.clear()
            res3 = assistant.send_ipc({"op": "stop"})
            self.assertTrue(res3 and res3["ok"])
            self.assertTrue(assistant._stop_speech_event.is_set())
        finally:
            if assistant._lock:
                assistant._lock.close()
            assistant._lock = orig_lock
            assistant.IPC_PORT = orig_port

    def test_webpage_reader_and_skip_dirs(self):
        self.assertIn("http or https", nova_skills.read_webpage("file:///etc/passwd").lower())

        class FakeResp:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
            def read(self, n):
                return b"<html><head><style>x{}</style><script>bad()</script></head><body><h1>Nova</h1><p>Hello world.</p></body></html>"

        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            txt = nova_skills.read_webpage("https://example.com")
        self.assertEqual(txt, "Nova Hello world.")

        doc_dir = Path(self.tmp.name) / "Documents"
        nm_dir = doc_dir / "node_modules" / "pkg"
        real_dir = doc_dir / "work"
        nm_dir.mkdir(parents=True)
        real_dir.mkdir(parents=True)
        (nm_dir / "invoice.txt").write_text("skip")
        (real_dir / "invoice_real.txt").write_text("keep")
        orig_home, orig_dirs = nova_skills.HOME, nova_skills.SEARCH_DIRS
        try:
            nova_skills.HOME = Path(self.tmp.name)
            nova_skills.SEARCH_DIRS = ("Documents",)
            hits = nova_skills.search_files(["invoice"])
            self.assertEqual([h.name for h in hits], ["invoice_real.txt"])
        finally:
            nova_skills.HOME, nova_skills.SEARCH_DIRS = orig_home, orig_dirs

    def test_top_processes_dynamic_close_and_briefing(self):
        sample_procs = [("chrome.exe", "1", 512000), ("CustomApp.exe", "2", 102400)]
        with mock.patch.object(nova_skills, "_running_procs", return_value=sample_procs):
            top = assistant.handle("what's using the most memory")
            self.assertIn("chrome", top.lower())
            with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as r:
                res = assistant.handle("close customapp")
                self.assertEqual(res, "Closing customapp.")
                r.assert_called_once()
                self.assertEqual(r.call_args.args[0], ["taskkill", "/IM", "CustomApp.exe"])

        with mock.patch.object(nova_skills, "_battery_info", return_value=(20, False, 1800)), \
             mock.patch.object(nova_skills, "weather", return_value="Clear, 22 degrees."):
            b = nova_skills.briefing()
            self.assertIn("20 percent and unplugged", b.lower())

    def test_routines_lifecycle_and_recursion_guard(self):
        self.assertIn("focus mode", assistant.handle("list routines").lower())
        res = assistant.handle("create routine study time to add task review notes and set a timer for 25 minutes")
        self.assertIn("saved routine study time", res.lower())
        out = assistant.handle("study time")
        self.assertIn("review notes", out.lower())
        self.assertIn("25 minutes", out.lower())
        # Prevent infinite self-recursion
        nova_skills.save_routine("loop test", ["run routine loop test"])
        loop_out = nova_skills.run_routine("loop test")
        self.assertIn("stopped", loop_out.lower())
        self.assertIn("deleted", assistant.handle("delete routine study time").lower())

    def test_git_github_and_confirmed_shell(self):
        gs = assistant.handle("git status")
        self.assertIn("branch main", gs.lower())
        self.assertIn("couldn't find", nova_skills.git_status("nonexistent_repo_xyz").lower())

        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="not logged in")):
            self.assertIn("gh auth login", assistant.handle("github status").lower())

        # Shell command must ask confirmation first, never run unconfirmed
        with mock.patch.object(nova_skills, "_exec_shell", return_value="hi") as m:
            prompt = assistant.handle("please run command echo hi")
            self.assertIn("run command echo hi", prompt.lower())
            m.assert_not_called()
            reply = assistant.handle("yes")
            self.assertEqual(reply, "hi")
            m.assert_called_once_with("echo hi")

    def test_mic_recalibrate_and_low_peak_gate(self):
        import numpy as np
        mic = assistant.Mic.__new__(assistant.Mic)
        mic.np = np
        mic.sr = 16000
        mic.ambient = 0.01
        mic.model = mock.Mock()
        mic.voice_lock = None
        quiet_pulse = np.full((1600, 1), 0.005, dtype=np.float32)
        silent = np.zeros((1600, 1), dtype=np.float32)

        class FakeStream:
            read_available = 0
            def __init__(self, items):
                self.items = list(items)
            def read(self, n):
                if len(self.items) == 1 and n == 16000:
                    return self.items[0]
                return self.items.pop(0)

        mic.ambient = 0.0005
        mic.stream = FakeStream([(quiet_pulse, False)] * 5 + [(silent, False)] * 12)
        self.assertEqual(mic.listen(wait=5), "")
        mic.model.transcribe.assert_not_called()

        mic.stream = FakeStream([(np.full((16000, 1), 0.02, dtype=np.float32), False)])
        self.assertEqual(mic.recalibrate(), "Microphone recalibrated.")
        self.assertAlmostEqual(mic.ambient, 0.02, places=4)

    def test_calendar_ics_events_alerts_and_quick_lookup(self):
        from datetime import datetime, timedelta
        ics = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:ev-1\r\n"
            "SUMMARY:Project Sync\\, Team\r\n  Part 2\r\nLOCATION:Room B\r\n"
            "DTSTART:20261201T143000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        evs = nova_calendar.parse_ics(ics)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["title"], "Project Sync, Team Part 2")
        self.assertEqual(evs[0]["location"], "Room B")

        soon = datetime.now() + timedelta(minutes=5)
        nova_calendar.add_event("Standup call", soon, duration_mins=30)
        summary = assistant.handle("what's on my calendar today")
        self.assertIn("standup call", summary.lower())
        with mock.patch.object(nova_skills, "weather", return_value=""):
            self.assertIn("standup call", nova_skills.briefing().lower())

        alerts1 = nova_calendar.due_meeting_alerts(10)
        alerts2 = nova_calendar.due_meeting_alerts(10)
        self.assertEqual(len(alerts1), 1)
        self.assertIn("standup call", alerts1[0].lower())
        self.assertEqual(alerts2, [])

        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(nova_skills, "_http_json", return_value={
                 "extract": "Ada Lovelace was an English mathematician. She worked on the Analytical Engine. Extra sentence."
             }):
            ans = assistant.handle("who is Ada Lovelace")
        self.assertEqual(ans, "Ada Lovelace was an English mathematician. She worked on the Analytical Engine.")

    def test_voice_mishearing_filler_wake_and_cancel(self):
        self.assertEqual(assistant.find_wake("ok hey can you nova open notepad"), "open notepad")
        self.assertIsNone(assistant.find_wake("one two three four five nova open notepad"))

        with mock.patch.object(nova_skills, "launch_any", return_value=True) as lm:
            r1 = assistant.handle("Open WhatsApp. Hello.")
        self.assertEqual(r1, "Opening whatsapp.")
        lm.assert_called_once_with("whatsapp:")

        with mock.patch.object(nova_skills, "IS_WIN", True), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as sr:
            r2 = assistant.handle("Close what's up.")
        self.assertEqual(r2, "Closing what's up.")
        self.assertEqual(sr.call_args.args[0], ["taskkill", "/IM", "WhatsApp.exe"])

        with mock.patch.object(nova_skills, "media", return_value="Going back.") as mm:
            r3 = assistant.handle("And go back.")
        self.assertEqual(r3, "Going back.")
        mm.assert_called_once_with("prev")

        assistant.ask("Which one?", lambda x: "picked " + x)
        self.assertEqual(assistant.handle("You forget about that one."), "Okay, cancelled.")

    def test_mic_vad_gate_and_ambient_cap(self):
        import numpy as np
        mic = assistant.Mic.__new__(assistant.Mic)
        mic.np = np
        mic.sr = 16000
        mic.ambient = 0.014
        mic._vad = True
        mic.model = mock.Mock()
        mic.voice_lock = None

        loud_chunk = np.full((1600, 1), 0.08, dtype=np.float32)
        noise_chunk = np.full((1600, 1), 0.03, dtype=np.float32)
        silent_chunk = np.zeros((1600, 1), dtype=np.float32)

        class FakeStream:
            read_available = 0

            def __init__(self, items):
                self.items = list(items)

            def read(self, n):
                return self.items.pop(0)

        mic.stream = FakeStream([(noise_chunk, False)] * 3 + [(loud_chunk, False)] * 5 + [(silent_chunk, False)] * 10)
        with mock.patch("faster_whisper.vad.get_speech_timestamps", return_value=[]) as gv:
            out = mic.listen(wait=5)
        self.assertEqual(out, "")
        self.assertLessEqual(mic.ambient, 0.015)
        gv.assert_called_once()
        mic.model.transcribe.assert_not_called()

    def test_ai_billing_cooldown_and_offline_fallback(self):
        nova_skills.CONFIG_FILE.write_text(json.dumps({
            "provider": "ollama_cloud", "api_key": "ol-key", "model": "deepseek-v4.1-flash"
        }))
        with mock.patch.object(nova_skills, "_http_json", side_effect=RuntimeError("HTTP 402: credits needed")) as m:
            first = assistant.handle("explain quantum entanglement")
        self.assertIn("credits", first.lower())
        self.assertTrue(nova_skills.ai_blocked())
        self.assertEqual(m.call_count, 1)

        with mock.patch.object(nova_skills, "_http_json", side_effect=[{"extract": "Python is a language."}]) as m2:
            fact = assistant.handle("who is Python")
        self.assertEqual(fact, "Python is a language.")
        self.assertEqual(m2.call_count, 1)

        with mock.patch.object(nova_skills, "_http_json", side_effect=AssertionError("AI should be on cooldown")):
            silent = assistant.handle("this is a long random conversation phrase with more than ten words right now")
        self.assertIsNone(silent)

    def test_window_shortcuts_status_queries_and_clear_clipboard(self):
        with mock.patch.object(nova_skills, "IS_WIN", True), \
             mock.patch.object(nova_skills, "press") as kp:
            self.assertEqual(assistant.handle("maximize window"), "Maximized window.")
            self.assertEqual(assistant.handle("snap window left"), "Snapped window left.")
            self.assertEqual(assistant.handle("new tab"), "Opened a new tab.")
            self.assertEqual(assistant.handle("close tab"), "Closed the tab.")
            self.assertEqual(assistant.handle("undo typing"), "Undid the last edit.")
            self.assertEqual(assistant.handle("scroll down"), "Scrolled down.")
        self.assertEqual(
            [c.args for c in kp.call_args_list],
            [("win", "up_arrow"), ("win", "left_arrow"), ("ctrl", "t"), ("ctrl", "w"), ("ctrl", "z"), ("pgdn",)],
        )

        with mock.patch.object(nova_skills, "IS_WIN", True), \
             mock.patch.object(nova_skills, "_audio_cmd", return_value=(42, False)) as ac:
            self.assertEqual(assistant.handle("what's the volume"), "Volume is at 42 percent.")
        ac.assert_called_once_with("get", 0)

        with mock.patch.object(nova_skills, "IS_WIN", True), \
             mock.patch.object(nova_skills, "ps", return_value=SimpleNamespace(stdout="75\n")) as psm:
            self.assertEqual(assistant.handle("what's the brightness"), "Brightness is at 75 percent.")
            self.assertEqual(assistant.handle("clear clipboard"), "Clipboard cleared.")
        self.assertEqual(psm.call_count, 2)

        with mock.patch.object(nova_skills, "IS_WIN", True), \
             mock.patch.object(nova_skills, "_running_procs", return_value=[
                 ("svchost.exe", "1", 90000), ("chrome.exe", "2", 50000), ("Code.exe", "3", 30000)
             ]):
            apps = assistant.handle("what apps are running")
        self.assertIn("chrome", apps.lower())
        self.assertIn("code", apps.lower())
        self.assertNotIn("svchost", apps.lower())
        self.assertIn("ip address is", assistant.handle("what's my ip address").lower())

    def test_offline_math_and_unit_conversions(self):
        self.assertEqual(assistant.handle("what is 15 percent of 240"), "15 percent of 240 is 36.")
        self.assertEqual(assistant.handle("20% off 80"), "20 percent off 80 is 64.")
        self.assertEqual(assistant.handle("what is the square root of 144"), "The square root of 144 is 12.")
        self.assertEqual(assistant.handle("what is (12 + 8) * 3"), "The answer is 60.")
        self.assertEqual(assistant.handle("12 times 15"), "The answer is 180.")
        self.assertEqual(assistant.handle("convert 100 km to miles"), "100 kilometers is 62.1371 miles.")
        self.assertEqual(assistant.handle("72 fahrenheit to celsius"), "72 degrees Fahrenheit is 22.2222 degrees Celsius.")
        self.assertEqual(assistant.handle("64 gigabytes in megabytes"), "64 gigabytes is 65536 megabytes.")

    def test_user_name_persistence_and_personalization(self):
        self.assertIn("haven't told me", assistant.handle("what's my name").lower())
        res = assistant.handle("my name is COBBY")
        self.assertEqual(res, "Got it, Cobby. I'll remember your name.")
        self.assertEqual(assistant.handle("what's my name"), "Your name is Cobby.")
        self.assertEqual(assistant.handle("who am i"), "Your name is Cobby.")
        self.assertIn("Cobby", assistant.handle("hello"))
        self.assertIn("Cobby", assistant._wake_reply())
        self.assertIn("The user's name is Cobby.", nova_skills._system())
        with mock.patch.object(nova_skills, "weather", return_value=""):
            self.assertIn("Cobby", assistant.handle("good morning"))
        # Negative guard: "call me back" must not overwrite the saved user name
        with mock.patch.object(nova_skills, "ask_ai", return_value=None):
            assistant.handle("call me back")
        self.assertEqual(nova_skills.get_user_name(), "Cobby")

    def test_customize_assistant_name_multiuser_and_reset(self):
        self.assertEqual(nova_skills.get_assistant_name(), "Nova")
        reply = assistant.handle("change your name to Jarvis")
        self.assertIn("jarvis", reply.lower())
        self.assertEqual(nova_skills.get_assistant_name(), "Jarvis")
        self.assertEqual(assistant.find_wake("hey jarvis open notepad"), "open notepad")
        self.assertIsNone(assistant.find_wake("nova open notepad"))
        self.assertIn("i'm jarvis", assistant.handle("who are you").lower())
        self.assertIn("you are jarvis", nova_skills._system().lower())
        with self.assertRaises(SystemExit):
            assistant.handle("quit jarvis")
        with mock.patch.object(nova_skills, "weather", return_value=""):
            self.assertIn("good ", assistant.handle("good morning jarvis").lower())

        assistant.handle("my name is Cobby")
        assistant.handle("switch user to Alex")
        self.assertEqual(nova_skills.get_user_name(), "Alex")
        users_msg = assistant.handle("list users")
        self.assertIn("Cobby", users_msg)
        self.assertIn("Alex", users_msg)

        nova_skills.update_profile(assistant_name="Atlas", voice_engine="sapi", voice_name="en-US-GuyNeural")
        prof = nova_skills.get_profile()
        self.assertEqual(prof["assistant_name"], "Atlas")
        self.assertEqual(prof["voice_engine"], "sapi")
        self.assertEqual(prof["voice_name"], "en-US-GuyNeural")

        nova_skills.reset_personal_data()
        self.assertEqual(nova_skills.get_assistant_name(), "Nova")
        self.assertEqual(nova_skills.get_user_name(), "")

    def test_app_packager_and_installer_isolation(self):
        import zipfile
        import build_app
        import install_nova

        with tempfile.TemporaryDirectory() as pkg_tmp:
            staged = build_app.stage_clean_files(Path(pkg_tmp) / "stage")
            self.assertTrue((staged / "Install_Nova.bat").exists())
            self.assertFalse((staged / ".env").exists())
            z = build_app.build_portable_zip(staged, out_dir=Path(pkg_tmp) / "out")
            with zipfile.ZipFile(z) as zf:
                names = set(zf.namelist())
            self.assertIn("NovaAssistant/assistant.py", names)
            self.assertIn("NovaAssistant/Install_Nova.bat", names)
            self.assertNotIn("NovaAssistant/.env", names)

            dest = Path(pkg_tmp) / "install_dest"
            sm_dir = Path(pkg_tmp) / "StartMenu"
            desk_lnk = Path(pkg_tmp) / "Desktop" / "Nova Assistant.lnk"
            desk_lnk.parent.mkdir(parents=True, exist_ok=True)
            vbs = Path(pkg_tmp) / "Startup" / "Nova.vbs"
            with mock.patch.object(install_nova, "start_menu_dir", return_value=sm_dir), \
                 mock.patch.object(install_nova, "desktop_shortcut_path", return_value=desk_lnk), \
                 mock.patch.object(install_nova, "startup_vbs_path", return_value=vbs), \
                 mock.patch.object(install_nova, "create_shortcut", side_effect=lambda p, *a, **k: p.parent.mkdir(parents=True, exist_ok=True) or p.write_text("lnk")), \
                 mock.patch.object(install_nova, "register_uninstall") as reg, \
                 mock.patch("subprocess.Popen"):
                out_dest = install_nova.install_app(dest_dir=dest, autostart=True, desktop=True, launch_now=False)
            self.assertEqual(out_dest, dest.resolve())
            self.assertTrue((dest / "assistant.py").exists())
            self.assertTrue((sm_dir / "Customize Assistant.lnk").exists())
            self.assertTrue(desk_lnk.exists())
            self.assertTrue(vbs.exists())
            reg.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
