"""The Claude Code backend, with the CLI replaced by a fake so nothing is actually run."""
import json
import unittest
from types import SimpleNamespace

from papertrade.brain import BrainUnavailable, ClaudeCodeBrain, make_brain, ClaudeBrain
from papertrade.desk import REPORT_SCHEMA


class FakeCLI:
    def __init__(self, stdout="", stderr="", code=0, error=None):
        self.stdout, self.stderr, self.code, self.error, self.calls = stdout, stderr, code, error, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if self.error:
            raise self.error
        return SimpleNamespace(stdout=self.stdout, stderr=self.stderr, returncode=self.code)


def result(**fields):
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "",
            "usage": {"input_tokens": 1200, "output_tokens": 300}}
    base.update(fields)
    return json.dumps(base)


class ClaudeCodeTest(unittest.TestCase):
    def test_command_and_structured_output(self):
        cli = FakeCLI(result(structured_output={"summary": "ok", "lessons": []}))
        brain = ClaudeCodeBrain(runner=cli, command="claude")
        answer, problem, usage = brain.ask("SYSTEM", "the briefing", REPORT_SCHEMA)
        self.assertEqual((answer, problem, usage["input_tokens"]), ({"summary": "ok", "lessons": []}, "", 1200))
        cmd, kw = cli.calls[0]
        self.assertTrue(cmd[0].endswith("claude"))
        cached = FakeCLI(result(structured_output={"summary": "ok"}, usage={
            "input_tokens": 24, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 1000, "output_tokens": 5}))
        self.assertEqual(ClaudeCodeBrain(runner=cached, command="claude").ask("S", "m", REPORT_SCHEMA)[2],
                         {"input_tokens": 10024, "output_tokens": 5})
        self.assertEqual(cmd[1], "-p")
        self.assertEqual(json.loads(cmd[cmd.index("--json-schema") + 1]), REPORT_SCHEMA)
        self.assertEqual(cmd[cmd.index("--system-prompt") + 1], "SYSTEM")
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")          # no tools: it only reads and answers
        self.assertEqual(kw["input"], "the briefing")                # long briefings go via stdin
        self.assertNotIn("ANTHROPIC_API_KEY", kw["env"])             # never falls back to API billing

    def test_text_result_fallback(self):
        cli = FakeCLI(result(result='```json\n{"a": 1}\n```'))
        self.assertEqual(ClaudeCodeBrain(runner=cli).ask("s", "m", {})[0], {"a": 1})

    def test_problems_are_reported_not_raised(self):
        for cli in (FakeCLI(result(result="not json")), FakeCLI(result(is_error=True, result="overloaded")),
                    FakeCLI(error=TimeoutError("slow"))):
            answer, problem, _ = ClaudeCodeBrain(runner=cli).ask("s", "m", {})
            self.assertIsNone(answer)
            self.assertTrue(problem)

    def test_news_text_mentioning_limits_is_not_an_error(self):
        cli = FakeCLI(result(structured_output={"headline": "Exchange hits rate limit reached by traders"}))
        self.assertIsNotNone(ClaudeCodeBrain(runner=cli).ask("s", "m", {})[0])

    def test_stop_conditions(self):
        for cli in (FakeCLI(error=FileNotFoundError()),
                    FakeCLI(result(is_error=True, result="Not logged in · Please run /login"), code=1),
                    FakeCLI(result(is_error=True, result="Claude usage limit reached"), code=1)):
            with self.assertRaises(BrainUnavailable):
                ClaudeCodeBrain(runner=cli).ask("s", "m", {})

    def test_default_backend_is_the_subscription(self):
        self.assertIsInstance(make_brain(), ClaudeCodeBrain)
        self.assertEqual(make_brain().model, "opus")


if __name__ == "__main__":
    unittest.main()
