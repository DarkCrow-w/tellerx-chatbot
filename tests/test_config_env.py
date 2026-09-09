from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from app.core.config import Settings


class ModelTokenEnvironmentTest(unittest.TestCase):
    """确保模型 Token 只从环境变量进入运行配置。"""

    def test_model_api_key_is_read_from_environment_and_excluded_from_dump(self) -> None:
        with patch.dict("os.environ", {"MODEL_API_KEY": "  internal-token  "}, clear=False):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.require_model_api_key(), "internal-token")
        self.assertNotIn("model_api_key", settings.model_dump())
        self.assertNotIn("internal-token", repr(settings))

    def test_missing_model_api_key_has_actionable_error(self) -> None:
        with patch.dict(
            "os.environ",
            {"MODEL_API_KEY": "", "QWEN_API_KEY": ""},
            clear=False,
        ):
            settings = Settings(_env_file=None)

        with self.assertRaisesRegex(RuntimeError, "MODEL_API_KEY"):
            settings.require_model_api_key()

    def test_log_level_is_normalized_and_invalid_value_is_rejected(self) -> None:
        settings = Settings(_env_file=None, log_level="debug")
        self.assertEqual(settings.log_level, "DEBUG")

        with self.assertRaisesRegex(ValueError, "LOG_LEVEL"):
            Settings(_env_file=None, log_level="verbose")

    def test_qwen_embedding_defaults_to_2560_dimensions(self) -> None:
        # 清空进程环境，避免开发机上的旧配置掩盖代码默认值。
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.embedding_dimensions, 2560)

    def test_flash_native_dimensions_are_allowed_with_matching_schema(self) -> None:
        settings = Settings(_env_file=None, embedding_dimensions=1024)
        self.assertEqual(settings.embedding_dimensions, 1024)

    def test_postgresql_rejects_unsupported_dimensions(self) -> None:
        with self.assertRaisesRegex(ValueError, "1024 or 2560"):
            Settings(_env_file=None, embedding_dimensions=1536)

    def test_flash_does_not_silently_use_existing_2560_column(self) -> None:
        from app.integrations.search import SearchIndex

        index = SearchIndex(Settings(_env_file=None, embedding_dimensions=1024))
        connection = Mock()
        index.engine = Mock()
        index.engine.connect.return_value.__enter__ = Mock(return_value=connection)
        index.engine.connect.return_value.__exit__ = Mock(return_value=False)
        result = connection.execute.return_value.mappings.return_value.one
        result.return_value = {
            "table_ready": True,
            "vector_ready": True,
            "trgm_ready": True,
            "embedding_type": "halfvec(2560)",
        }
        with self.assertRaisesRegex(RuntimeError, "dimension mismatch"):
            index.ensure_index()
        result.return_value["embedding_type"] = "vector(1024)"
        index.ensure_index()


if __name__ == "__main__":
    unittest.main()
