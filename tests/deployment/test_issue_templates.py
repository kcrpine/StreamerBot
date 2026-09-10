"""The issue forms and the label that gets applied when no form is used.

Worth testing because the failure mode is invisible to the maintainer: GitHub
reports a malformed issue form only to the person trying to file with it, who
then has nowhere to report that the reporting form is broken. A label that does
not exist in the repository is dropped just as quietly.
"""

import unittest
from pathlib import Path
from unittest import TestCase

try:
    import yaml
except ImportError:  # pragma: no cover - host without pyyaml
    raise unittest.SkipTest("pyyaml is not installed on this host")

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"

# Created in the repository with gh label create. An issue form naming a label
# that does not exist silently files the issue without it.
DECLARED_LABELS = {"bug", "needs investigation", "other", "colab request"}


def load(name):
    path = TEMPLATES / name
    if not path.is_file():
        raise unittest.SkipTest(f"{name} is not present; host-only test skipped")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fields(form):
    return {f.get("id"): f for f in form["body"] if f.get("type") != "markdown"}


class FormValidityTests(TestCase):
    """The rules GitHub enforces, checked here so a filer never meets them."""

    def test_every_form_parses_and_is_shaped_like_a_form(self):
        for name in ("bug_report.yml", "collab_request.yml"):
            form = load(name)
            self.assertIn("name", form, name)
            self.assertIn("description", form, name)
            self.assertIsInstance(form["body"], list, name)

    def test_every_field_has_a_type_and_an_id(self):
        for name in ("bug_report.yml", "collab_request.yml"):
            form = load(name)
            for index, field in enumerate(form["body"]):
                self.assertIn("type", field, f"{name} body[{index}]")
                if field["type"] != "markdown":
                    self.assertIn("id", field, f"{name} body[{index}]")

    def test_every_field_has_a_label(self):
        """A field with no label is an unnamed edit box to a screen reader."""
        for name in ("bug_report.yml", "collab_request.yml"):
            form = load(name)
            for field in form["body"]:
                if field["type"] == "markdown":
                    continue
                self.assertTrue(
                    field.get("attributes", {}).get("label"),
                    f"{name}: {field['id']} has no label",
                )

    def test_no_form_asks_for_a_label_the_repository_does_not_have(self):
        for name in ("bug_report.yml", "collab_request.yml"):
            for label in load(name).get("labels", []):
                self.assertIn(label, DECLARED_LABELS, f"{name} wants {label!r}")

    def test_dropdown_options_are_few_enough_to_hear(self):
        """The project's own rule for numbered choices. A dropdown read aloud in
        full is the same problem as a long menu."""
        for name in ("bug_report.yml", "collab_request.yml"):
            form = load(name)
            for field in form["body"]:
                if field["type"] != "dropdown":
                    continue
                options = field["attributes"]["options"]
                self.assertLessEqual(
                    len(options), 15, f"{name}: {field['id']} has {len(options)} options"
                )


class BugFormTests(TestCase):
    def setUp(self):
        self.form = load("bug_report.yml")
        self.fields = fields(self.form)

    def test_it_asks_for_investigation(self):
        self.assertIn("needs investigation", self.form["labels"])

    def test_it_asks_whether_a_log_is_available(self):
        """The point of asking separately from the paste box: someone who has a
        log but cannot share it right now is worth knowing about, and a single
        empty textarea cannot say that."""
        self.assertIn("have-log", self.fields)
        options = self.fields["have-log"]["attributes"]["options"]
        joined = " ".join(options).lower()
        self.assertTrue(any(o.lower().startswith("yes") for o in options))
        self.assertIn("no", joined)

    def test_asking_for_a_log_is_not_compulsory(self):
        """Requiring it would turn "I do not have one" into a reason not to file
        at all."""
        for field_id in ("have-log", "log"):
            self.assertFalse(
                self.fields[field_id].get("validations", {}).get("required", False),
                f"{field_id} must not be required",
            )

    def test_it_says_where_the_log_lives(self):
        description = self.fields["have-log"]["attributes"]["description"]
        self.assertIn("StreamerBot.log", description)
        # Each bot logs into its own directory, which is not obvious.
        self.assertIn("bots/", description)

    def test_only_the_two_questions_anyone_can_answer_are_required(self):
        required = [
            fid for fid, f in self.fields.items()
            if f.get("validations", {}).get("required")
        ]
        self.assertEqual(sorted(required), ["expected", "what-happened"])

    def test_it_warns_against_pasting_a_portal_link(self):
        """A portal link is a bearer credential, and this issue is public."""
        text = " ".join(
            f["attributes"]["value"] for f in self.form["body"]
            if f["type"] == "markdown"
        ).lower()
        self.assertIn("public", text)
        self.assertIn("portal link", text)


class CollabFormTests(TestCase):
    def setUp(self):
        self.form = load("collab_request.yml")
        self.fields = fields(self.form)

    def test_it_is_labelled_as_a_collaboration_request(self):
        self.assertEqual(self.form["labels"], ["colab request"])

    def test_it_asks_for_everything_that_was_specified(self):
        for field_id in ("github-username", "email", "why", "ideas"):
            self.assertIn(field_id, self.fields)
            self.assertTrue(
                self.fields[field_id]["validations"]["required"],
                f"{field_id} should be required",
            )

    def test_the_email_field_says_the_issue_is_public(self):
        """Collecting an address in a public issue without saying so sets someone
        up for spam they did not agree to. The field stays required, and offers
        wording for anyone who would rather not."""
        description = self.fields["email"]["attributes"]["description"].lower()
        self.assertIn("public", description)
        self.assertIn("privately", description)

    def test_the_optional_question_is_actually_optional(self):
        self.assertFalse(
            self.fields["accessibility"]["validations"].get("required", False)
        )
        options = self.fields["accessibility"]["attributes"]["options"]
        self.assertEqual(options[0], "I would rather not say")


class UntemplatedLabellingTests(TestCase):
    def setUp(self):
        path = ROOT / ".github" / "workflows" / "label-untemplated-issues.yml"
        if not path.is_file():
            raise unittest.SkipTest("the labelling workflow is not present")
        self.text = path.read_text(encoding="utf-8")
        self.workflow = yaml.safe_load(self.text)

    def test_blank_issues_stay_enabled(self):
        """Otherwise there is nothing for this workflow to label, and someone
        whose problem fits no form has nowhere to put it."""
        config = load("config.yml")
        self.assertTrue(config["blank_issues_enabled"])

    def test_it_runs_when_an_issue_is_opened(self):
        # PyYAML reads a bare `on:` key as the boolean True.
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(triggers["issues"]["types"], ["opened"])

    def test_it_can_write_labels_and_nothing_more(self):
        self.assertEqual(self.workflow["permissions"]["issues"], "write")
        self.assertEqual(self.workflow["permissions"]["contents"], "read")

    def test_it_applies_the_other_label(self):
        self.assertIn("'other'", self.text)

    def test_no_issue_text_is_interpolated_into_a_shell_command(self):
        """The injection this repository already had once: a commit message
        interpolated into a run: block executed itself. An issue title is
        attacker-controlled in exactly the same way."""
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                run = step.get("run", "")
                self.assertNotIn("github.event", run)
                self.assertNotIn("${{", run)


if __name__ == "__main__":
    unittest.main()
