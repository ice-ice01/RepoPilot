"""纯标准库测试；不导入正式后端，也不加载其 conftest.py。"""

import asyncio
import unittest

from python_basics import Issue, SAMPLE_ISSUES, analyze_all, classify_issue, summarize_issues


class PythonBasicsTest(unittest.TestCase):
    def test_bug_classification(self):
        self.assertEqual(classify_issue("Login returns 500"), "Bug")
        self.assertEqual(classify_issue("Something happened", ["BUG"]), "Bug")

    def test_documentation_classification(self):
        self.assertEqual(classify_issue("更新文档"), "Documentation")

    def test_default_classification(self):
        self.assertEqual(classify_issue("新增主题", None), "Feature")

    def test_summary(self):
        self.assertEqual(
            summarize_issues(SAMPLE_ISSUES),
            {
                "total": 3,
                "open": 2,
                "closed": 1,
                "categories": {"Bug": 1, "Feature": 1},
            },
        )

    def test_empty_summary(self):
        self.assertEqual(summarize_issues([])["total"], 0)

    def test_default_lists_are_independent(self):
        first = Issue(number=1, title="A")
        second = Issue(number=2, title="B")
        first.labels.append("bug")
        self.assertEqual(second.labels, [])

    def test_issue_method(self):
        self.assertEqual(Issue(number=1, title="A").summary(), "#1 A")

    def test_async_results_keep_input_order(self):
        results = asyncio.run(analyze_all(SAMPLE_ISSUES))
        self.assertEqual([item["number"] for item in results], [101, 102, 103])
        self.assertEqual(results[0]["category"], "Bug")


if __name__ == "__main__":
    unittest.main()
