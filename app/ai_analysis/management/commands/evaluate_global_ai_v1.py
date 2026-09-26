import json

from django.core.management.base import BaseCommand, CommandError
from django.test.runner import DiscoverRunner

from ai_analysis.evaluation_cases import evaluation_summary


class Command(BaseCommand):
    help = "在临时测试数据库运行全局 AI v1 确定性评测；不会调用模型或写业务数据。"

    def add_arguments(self, parser):
        parser.add_argument("--list", action="store_true", help="只列出20个案例，不运行")
        parser.add_argument("--json", action="store_true", help="以 JSON 输出案例清单")
        parser.add_argument("--keepdb", action="store_true", help="保留测试数据库以便重复运行")

    def handle(self, *args, **options):
        summary = evaluation_summary()
        if options["list"]:
            self._write_summary(summary, as_json=options["json"])
            return

        runner = DiscoverRunner(
            verbosity=options["verbosity"],
            interactive=False,
            keepdb=options["keepdb"],
        )
        failures = runner.run_tests(["ai_analysis.tests_evaluation"])
        if failures:
            raise CommandError(f"确定性评测失败：{failures} 项测试未通过。")
        summary["deterministic_result"] = "passed"
        self._write_summary(summary, as_json=options["json"])

    def _write_summary(self, summary, *, as_json):
        if as_json:
            self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"全局 AI v1：共 {summary['total']} 例；当前可执行 "
                f"{summary['executable']} 例；待实现 {summary['pending']} 例；模型调用 0。"
            )
        )
        for case in summary["cases"]:
            suffix = f"；等待：{case['pending_on']}" if case["pending_on"] else ""
            self.stdout.write(
                f"{case['case_id']} [{case['status']}] {case['verifies']}{suffix}"
            )
