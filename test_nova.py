import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import assistant
import nova_memory
import nova_skills
import nova_voice


class NovaTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = nova_skills.CONFIG_FILE
        self._orig_rem = nova_skills.REM_FILE
        self._orig_notes = nova_skills.NOTES
        self._orig_tasks = assistant.TASKS_FILE
        self._orig_mem = nova_skills._mem
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        nova_skills.CONFIG_FILE = tmp_path / "config.json"
        nova_skills.REM_FILE = tmp_path / "reminders.json"
        nova_skills.NOTES = tmp_path / "Nova Notes.txt"
        assistant.TASKS_FILE = tmp_path / "tasks.json"
        nova_skills._mem = nova_memory.Memory(tmp_path / "memory.db")
        nova_skills.reset_ai()
        assistant.setup_skills()

    def tearDown(self):
        nova_skills.CONFIG_FILE = self._orig_cfg
        nova_skills.REM_FILE = self._orig_rem
        nova_skills.NOTES = self._orig_notes
        assistant.TASKS_FILE = self._orig_tasks
        nova_skills._mem = self._orig_mem
        nova_skills.reset_ai()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
