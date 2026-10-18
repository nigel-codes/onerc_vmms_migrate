from unittest import TestCase
from onerc_vmms_migrate.html_files import rewrite


class TestHistoricalHTML(TestCase):
    def test_local_links_rewritten_and_unknown_external_links_preserved(self):
        seen=[]
        def resolve(url):
            seen.append(url)
            return '/private/files/verified.pdf' if url=='/private/files/A & B.pdf' else None
        original='<a href="/private/files/A &amp; B.pdf">proof</a><img src="https://other.test/files/x"><img src=\'/files/missing.png\'>'
        result=rewrite(original,resolve)
        self.assertIn('href="/private/files/verified.pdf"',result)
        self.assertIn('src="https://other.test/files/x"',result)
        self.assertIn("src='/files/missing.png'",result)
        self.assertEqual(seen,['/private/files/A & B.pdf','/files/missing.png'])
