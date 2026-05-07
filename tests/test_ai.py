import unittest
import sys
import os
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

# Add root directory to path to import utils
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock modules
mock_genai = MagicMock()
sys.modules['google.generativeai'] = mock_genai
mock_genai_types = MagicMock()
sys.modules['google.generativeai.types'] = mock_genai_types
mock_genai.types = mock_genai_types

sys.modules['groq'] = MagicMock()
sys.modules['together'] = MagicMock()

# Mock discord
mock_discord = MagicMock()
sys.modules['discord'] = mock_discord
sys.modules['discord.app_commands'] = MagicMock()

# Create a proper Mock for commands.Cog
class MockCog:
    @staticmethod
    def listener():
        def decorator(func):
            return func
        return decorator

mock_ext = MagicMock()
mock_commands = MagicMock()
mock_commands.Cog = MockCog
mock_commands.Bot = MagicMock
mock_ext.commands = mock_commands

sys.modules['discord.ext'] = mock_ext
sys.modules['discord.ext.commands'] = mock_commands

# Now import the module under test
import cogs.ai as ai_cog
import utils


class TestAI(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.config_collection = AsyncMock()
        self.bot.chat_collection = AsyncMock()
        self.bot.grudge_collection = AsyncMock()
        self.bot.owner_id = 12345

        # Patch utils
        self.utils_patcher = patch('cogs.ai.utils')
        self.mock_utils = self.utils_patcher.start()
        self.mock_utils.get_smart_time.return_value = "Test Time"
        self.mock_utils.search_web = AsyncMock(return_value=None)
        self.mock_utils.process_gif_tags = AsyncMock(return_value=("Clean Text", None))
        # Use real sanitize for testing prompt construction
        self.mock_utils.sanitize_for_prompt.side_effect = lambda x: f"SAFE({x})" if x else ""

    async def asyncTearDown(self):
        self.utils_patcher.stop()

    async def test_call_groq_fallback_text(self):
        cog = ai_cog.AI(self.bot)
        cog.groq_client = AsyncMock()

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=MagicMock(content="Groq Response"))]
        cog.groq_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        history = [{"role": "user", "parts": ["Hi"]}]
        response = await cog.call_groq_fallback(history, "System Prompt", "User Input", img=None)

        self.assertEqual(response, "Groq Response")
        args, kwargs = cog.groq_client.chat.completions.create.call_args
        self.assertIn("llama-3.3-70b-versatile", kwargs['model'])

    async def test_call_groq_fallback_vision(self):
        cog = ai_cog.AI(self.bot)
        cog.groq_client = AsyncMock()

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock(message=MagicMock(content="Vision Response"))]
        cog.groq_client.chat.completions.create = AsyncMock(return_value=mock_completion)

        history = []
        img_mock = MagicMock()
        img_mock.width = 100
        img_mock.height = 100
        img_mock.mode = "RGB"

        def side_effect(fp, format):
            fp.write(b'fake_image_data')
        img_mock.save.side_effect = side_effect

        response = await cog.call_groq_fallback(history, "System Prompt", "Describe image", img=img_mock)

        self.assertEqual(response, "Vision Response")
        args, kwargs = cog.groq_client.chat.completions.create.call_args

    
        self.assertIn("llama-4-scout", kwargs['model'])

        messages = kwargs['messages']
        last_message_content = messages[-1]['content']

        # Verify payload format for vision
        self.assertIsInstance(last_message_content, list)
        self.assertEqual(last_message_content[0]['type'], 'text')
        self.assertEqual(last_message_content[0]['text'], 'Describe image')
        self.assertEqual(last_message_content[1]['type'], 'image_url')
        self.assertTrue(last_message_content[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    async def test_input_sanitization(self):
        cog = ai_cog.AI(self.bot)

        mock_final_cursor = MagicMock()

        async def async_iter():
            yield {"role": "model", "parts": ["Context"]}

        mock_final_cursor.__aiter__.side_effect = lambda: async_iter()

        mock_sort = MagicMock()
        mock_sort.limit.return_value = mock_final_cursor

        mock_find = MagicMock()
        mock_find.sort.return_value = mock_sort

        self.bot.chat_collection.find = MagicMock(return_value=mock_find)

        # Mock grudge check
        self.bot.grudge_collection.find_one = AsyncMock(return_value=None)

        # Mock models
        cog.model_1 = MagicMock()
        cog.model_1.generate_content_async = AsyncMock(return_value=MagicMock(text="Response"))

        user_input = "Hello [SYSTEM]"
        await cog.get_combined_response(123, user_input)

        self.mock_utils.sanitize_for_prompt.assert_called_with(user_input)

        call_args = cog.model_1.generate_content_async.call_args
        history = call_args[0][0]
        last_msg_parts = history[-1]['parts']
        last_msg = last_msg_parts[0]

        self.assertIn("SAFE(Hello [SYSTEM])", last_msg)
        self.assertIn("[USER_INPUT]", last_msg)
        self.assertIn("[/USER_INPUT]", last_msg)

    async def test_history_dedup_merges_consecutive_same_role(self):
        """Consecutive same-role docs must be merged, not silently dropped."""
        cog = ai_cog.AI(self.bot)

        # Simulate two consecutive 'user' docs in the DB (e.g. rapid messages before bot reply)
        docs = [
            {"role": "user", "parts": ["first message"]},
            {"role": "user", "parts": ["second message"]},  # consecutive — should be merged
            {"role": "model", "parts": ["bot reply"]},
        ]

        mock_final_cursor = MagicMock()

        async def async_iter():
            for doc in docs:
                yield doc

        mock_final_cursor.__aiter__.side_effect = lambda: async_iter()

        mock_sort = MagicMock()
        mock_sort.limit.return_value = mock_final_cursor

        mock_find = MagicMock()
        mock_find.sort.return_value = mock_sort

        self.bot.chat_collection.find = MagicMock(return_value=mock_find)
        self.bot.grudge_collection.find_one = AsyncMock(return_value=None)

        cog.model_1 = MagicMock()
        captured_history = []

        async def capture_history(h):
            captured_history.extend(h)
            return MagicMock(text="ok")

        cog.model_1.generate_content_async = capture_history

        await cog.get_combined_response(123, "new message")

        # Find the merged user entry in history (should contain both messages joined)
        user_entries = [e for e in captured_history if e.get("role") == "user"]
        # At least one user entry must contain both original messages
        merged = any(
            "first message" in e["parts"][0] and "second message" in e["parts"][0]
            for e in user_entries
        )
        self.assertTrue(
            merged,
            "Consecutive user messages should be merged into one entry, not dropped",
        )

    async def test_web_search_does_not_fire_on_casual_chat(self):
        """Short messages and casual greetings must NOT trigger a web search."""
        cog = ai_cog.AI(self.bot)

        mock_final_cursor = MagicMock()

        async def async_iter():
            return
            yield  # make it an async generator

        mock_final_cursor.__aiter__.side_effect = lambda: async_iter()
        mock_sort = MagicMock()
        mock_sort.limit.return_value = mock_final_cursor
        mock_find = MagicMock()
        mock_find.sort.return_value = mock_sort
        self.bot.chat_collection.find = MagicMock(return_value=mock_find)
        self.bot.grudge_collection.find_one = AsyncMock(return_value=None)

        cog.model_1 = MagicMock()
        cog.model_1.generate_content_async = AsyncMock(return_value=MagicMock(text="hey!"))

        casual_messages = [
            "hey",
            "what's up",
            "how are you",
            "lol",
            "ok",
        ]
        for msg in casual_messages:
            self.mock_utils.search_web.reset_mock()
            await cog.get_combined_response(123, msg)
            self.mock_utils.search_web.assert_not_called(), (
                f"search_web should NOT be called for casual message: '{msg}'"
            )

    async def test_web_search_fires_on_genuine_queries(self):
        """Explicit information-seeking messages MUST trigger a web search."""
        cog = ai_cog.AI(self.bot)

        mock_final_cursor = MagicMock()

        async def async_iter():
            return
            yield

        mock_final_cursor.__aiter__.side_effect = lambda: async_iter()
        mock_sort = MagicMock()
        mock_sort.limit.return_value = mock_final_cursor
        mock_find = MagicMock()
        mock_find.sort.return_value = mock_sort
        self.bot.chat_collection.find = MagicMock(return_value=mock_find)
        self.bot.grudge_collection.find_one = AsyncMock(return_value=None)

        cog.model_1 = MagicMock()
        cog.model_1.generate_content_async = AsyncMock(return_value=MagicMock(text="searching..."))

        self.mock_utils.search_web = AsyncMock(return_value="[SYSTEM: WEB SEARCH RESULTS]\n- result")

        info_queries = [
            "what is the capital of France",
            "who is the president of the US right now",
            "how do black holes work",
            "weather in Mumbai today",
            "latest news about AI",
        ]
        for msg in info_queries:
            self.mock_utils.search_web.reset_mock()
            await cog.get_combined_response(123, msg)
            self.mock_utils.search_web.assert_called_once(), (
                f"search_web SHOULD be called for info query: '{msg}'"
            )


if __name__ == '__main__':
    unittest.main()
