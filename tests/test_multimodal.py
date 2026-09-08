import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("multimodal", Path(__file__).resolve().parents[1] / "scripts/multimodal.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MultimodalTests(unittest.TestCase):
    def sample(self):
        return {"changed": ["S2"], "budget_remaining": 6, "shots": [
            {"id": sid, "depends_on": deps, "cost_per_attempt": 2, "attempts_remaining": 1}
            for sid, deps in [("S1", []), ("S2", []), ("S3", ["S2"]), ("S4", ["S3"]), ("S5", [])]]}

    def test_transitive_repair_and_unaffected_shots(self):
        result = module.repair_plan(self.sample())
        self.assertEqual(result["recheck_or_rebuild"], ["S2", "S3", "S4"])
        self.assertEqual(result["unaffected_by_declared_changes"], ["S1", "S5"])
        self.assertEqual(result["one_attempt_estimate"], 6)
        self.assertFalse(result["executes_generation"])

    def test_budget_and_attempts_gate_plan(self):
        for key in ("budget", "attempts"):
            data = self.sample()
            if key == "budget": data["budget_remaining"] = 5
            else: data["shots"][2]["attempts_remaining"] = 0
            self.assertEqual(module.repair_plan(data)["status"], "NEEDS_PRODUCTION_DECISION")

    def test_no_change_does_not_request_generation(self):
        data = self.sample()
        data["changed"] = []
        self.assertEqual(module.repair_plan(data)["status"], "NO_CHANGE")

    def test_decimal_budget_boundary_is_not_false_overspend(self):
        data = self.sample()
        data["budget_remaining"] = 0.3
        for shot in data["shots"]: shot["cost_per_attempt"] = 0.1
        result = module.repair_plan(data)
        self.assertEqual(result["status"], "READY_FOR_REVIEW")
        self.assertEqual(result["one_attempt_estimate"], 0.3)

    def test_cycles_unknown_and_duplicate_ids_fail(self):
        for case in ("cycle", "unknown", "duplicate", "changed"):
            data = self.sample()
            if case == "cycle": data["shots"][1]["depends_on"] = ["S4"]
            if case == "unknown": data["shots"][1]["depends_on"] = ["S9"]
            if case == "duplicate": data["shots"][1]["id"] = "S1"
            if case == "changed": data["changed"] = ["S8"]
            with self.assertRaises(ValueError): module.repair_plan(data)

    def test_cost_and_budget_must_be_finite_numbers(self):
        for value in (True, -1, float("nan"), float("inf")):
            data = self.sample()
            data["budget_remaining"] = value
            with self.assertRaises(ValueError): module.repair_plan(data)

    def test_open_freeze_is_not_assigned_an_invented_end(self):
        result = module.parse_metadata("frame:0 pts:0 pts_time:0\nlavfi.scd.time=0.8\nlavfi.freezedetect.freeze_start=1.2\n")
        self.assertEqual(result["cut_candidates"], [0.8])
        self.assertEqual(result["low_change_intervals"], [{"start": 1.2, "end": None}])

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg optional for pixel test")
    def test_real_pixels_produce_transition_and_low_change_candidates(self):
        with tempfile.TemporaryDirectory(prefix="multimodal-test-") as temp:
            path = Path(temp) / "synthetic.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=black:s=64x64:r=10:d=1",
                            "-f", "lavfi", "-i", "color=white:s=64x64:r=10:d=1", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
                            "-c:v", "mpeg4", str(path)], check=True, capture_output=True)
            result = module.inspect_video(path, seconds=3, freeze_seconds=0.3)
            self.assertEqual(result["decoded_frames"], 20)
            self.assertTrue(any(abs(t - 1) < .11 for t in result["cut_candidates"]))
            self.assertTrue(result["low_change_intervals"])
            self.assertFalse(result["source_modified"])


if __name__ == "__main__":
    unittest.main()
