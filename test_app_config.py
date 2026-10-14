import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app_config import load_config, resolve_worksheet_names


class WorksheetConfigurationTests(unittest.TestCase):
    def settings(self, unmatched, ambiguous):
        return {
            "WORKSHEET_NAME_MAIN": "Сентябрь вторая партия_299_test",
            "WORKSHEET_NAME_UNMATCH": unmatched,
            "WORKSHEET_NAME_AMBIG": ambiguous,
        }

    def test_python_style_references_from_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                'GSERVICE_JSON=key.json\nGSHEET_ID=test\n'
                'WORKSHEET_NAME_MAIN="Сентябрь вторая партия_299_test"\n'
                'WORKSHEET_NAME_UNMATCH=WORKSHEET_NAME_MAIN+ "_непривязавшиеся"\n'
                'WORKSHEET_NAME_AMBIG=WORKSHEET_NAME_MAIN + "_неоднозначности"\n',
                encoding="utf-8-sig",
            )
            (root / ".req").write_text(
                'ORACLE_USER=user\nORACLE_DSN=test\nOPENAI_API_KEY=key\n'
                'ORACLE_PASSWORD="literal ${WORKSHEET_NAME_MAIN} # $password"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"APP_CONFIG_DIR": directory}):
                result = load_config()
            self.assertEqual(result["WORKSHEET_NAME_UNMATCH"], result["WORKSHEET_NAME_MAIN"] + "_непривязавшиеся")
            self.assertEqual(result["WORKSHEET_NAME_AMBIG"], result["WORKSHEET_NAME_MAIN"] + "_неоднозначности")
            self.assertEqual(result["ORACLE_PASSWORD"], "literal ${WORKSHEET_NAME_MAIN} # $password")
            self.assertEqual(result["GSERVICE_JSON"], str(root / "key.json"))

    def test_literal_names_and_braced_references(self):
        result = resolve_worksheet_names(self.settings("Готовое имя + суффикс", "${WORKSHEET_NAME_MAIN}_много"))
        self.assertEqual(result["WORKSHEET_NAME_UNMATCH"], "Готовое имя + суффикс")
        self.assertEqual(result["WORKSHEET_NAME_AMBIG"], result["WORKSHEET_NAME_MAIN"] + "_много")

    def test_cyclic_references_are_rejected(self):
        values = self.settings('WORKSHEET_NAME_AMBIG + "x"', 'WORKSHEET_NAME_UNMATCH + "y"')
        with self.assertRaisesRegex(RuntimeError, "Circular worksheet"):
            resolve_worksheet_names(values)

    def test_executable_expressions_are_rejected(self):
        values = self.settings('WORKSHEET_NAME_MAIN + __import__("os").getcwd()', "literal")
        with self.assertRaisesRegex(RuntimeError, "Unsupported worksheet"):
            resolve_worksheet_names(values)


if __name__ == "__main__":
    unittest.main()
