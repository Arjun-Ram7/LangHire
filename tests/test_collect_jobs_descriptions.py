import unittest
from unittest.mock import patch

from cli.collect_jobs import _apply_fetched_description

PROFILE_NEEDS_SPONSORSHIP = {"visa_sponsorship_needed": True}

JOB = {
    "title": "Software Engineer Intern",
    "company": "Honeywell Aerospace",
    "location": "Phoenix, AZ",
}


class ApplyFetchedDescriptionTests(unittest.TestCase):
    def test_saves_description_when_it_passes_constraints(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/1/",
                JOB,
                "We are looking for a software engineer to join our growing team.",
                PROFILE_NEEDS_SPONSORSHIP,
            )
        update_job.assert_called_once_with(
            "https://www.linkedin.com/jobs/view/1/",
            description="We are looking for a software engineer to join our growing team.",
        )

    def test_blocks_job_when_real_description_reveals_citizenship_requirement(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/2/",
                JOB,
                "Due to ITAR requirements, applicants must be a U.S. Person to be considered.",
                PROFILE_NEEDS_SPONSORSHIP,
            )
        update_job.assert_called_once_with(
            "https://www.linkedin.com/jobs/view/2/",
            status="blocked",
            error="Filtered out: requires U.S. citizenship",
        )

    def test_does_nothing_when_no_description_was_extracted(self):
        with patch("cli.collect_jobs.update_job") as update_job:
            _apply_fetched_description(
                "https://www.linkedin.com/jobs/view/3/", JOB, "", PROFILE_NEEDS_SPONSORSHIP
            )
        update_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
