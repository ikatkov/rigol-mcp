"""Reference retrieval and MCP transport tests, without contacting a scope."""

import asyncio
import hashlib
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import rigol_mcp
import rigol_reference as reference


class ManualTests(unittest.TestCase):
    def test_all_pages_and_source_provenance(self):
        self.assertEqual(len(reference.pages()), 260)
        self.assertEqual(reference.pages()[1], "")
        pdf = reference._reference_dir().joinpath("programming-guide.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), reference.manifest()["pdf_sha256"])
        for number, body in enumerate(reference.pages(), 1):
            self.assertIn(body, reference.read_pages(number))

    def test_abbreviated_and_long_commands_find_definition_before_toc(self):
        for query in (":WAV:PRE?", ":WAVeform:PREamble?", ":WAVEFORM:PREAMBLE?"):
            with self.subTest(query=query):
                result = reference.search_pages(query)
                self.assertEqual(result["results"][0]["pdf_page"], 242)
                self.assertEqual(result["results"][0]["printed_page"], "2-226")
        for query in (":CHAN4:SCAL?", ":CHANNEL4:SCALE?", ":CHANnel<n>:SCALe?"):
            with self.subTest(query=query):
                self.assertEqual(reference.search_pages(query)["results"][0]["pdf_page"], 30)
        for query in (":TIM:SCAL?", ":TIM:MAIN:SCAL?", ":TIMebase[:MAIN]:SCALe?"):
            with self.subTest(query=query):
                self.assertEqual(reference.search_pages(query)["results"][0]["pdf_page"], 173)

    def test_keywords_and_bounded_results(self):
        result = reference.search_pages("MATH fft", limit=2)
        self.assertEqual(len(result["results"]), 2)
        self.assertGreater(result["total_matching_pages"], 2)
        for hit in result["results"]:
            self.assertNotIn(hit["pdf_page"], range(5, 11))
            self.assertLessEqual(len(hit["snippet"]), 1800)
            self.assertEqual(hit["resource_uri"], f"rigol://manual/page/{hit['pdf_page']}")
        self.assertEqual(reference.search_pages("nonexistent_command_abc")["results"], [])

    def test_multiline_page_is_complete(self):
        page = reference.read_pages(141, 2)
        self.assertIn(":MEASure:ITEM? <item>[,<src>[,<src>]]", page)
        self.assertIn("## PDF page 142", page)
        self.assertIn("PVRMS", page)
        self.assertIn("Example", page)

    def test_invalid_ranges_and_queries(self):
        for start, count in ((0, 1), (-1, 1), (261, 1), (260, 2), (1, 0), (1, 6)):
            with self.subTest(start=start, count=count), self.assertRaises(ValueError):
                reference.read_pages(start, count)
        for query, limit in (("", 5), ("  ", 5), ("a" * 301, 5), ("FFT", 0), ("FFT", 11)):
            with self.subTest(query=query, limit=limit), self.assertRaises(ValueError):
                reference.search_pages(query, limit)
        with self.assertRaises(ValueError):
            rigol_mcp.get_manual(page_count=2)

    def test_lookup_does_not_use_a_socket(self):
        with patch.object(socket, "create_connection", side_effect=AssertionError("socket used")):
            self.assertIn("Cached edition:", rigol_mcp.get_manual())
            self.assertLess(len(rigol_mcp.get_manual()), 7000)
            self.assertIn("<xincrement>", rigol_mcp.get_manual(page=242))
            self.assertEqual(rigol_mcp.search_manual(":MEAS:ITEM?")["results"][0]["pdf_page"], 141)

    def test_missing_cache_explains_one_time_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(reference, "_reference_dir", return_value=Path(directory)):
                with self.assertRaises(ValueError) as raised:
                    reference._read_text("programming-guide.md")
                for text in (
                    "Required one-time manual download",
                    "not bundled",
                    "python scripts/cache_manual.py",
                    "pdftotext",
                    "internet access",
                ):
                    self.assertIn(text, str(raised.exception))


class MCPTransportTests(unittest.TestCase):
    def test_real_stdio_client_documentation(self):
        asyncio.run(self._check_stdio())

    async def _check_stdio(self):
        # An unrelated cwd and invalid host expose accidental local-path/socket dependencies.
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "import rigol_mcp; rigol_mcp.main()"],
            cwd="/tmp" if sys.platform != "win32" else str(Path.home()),
            env={
                "RIGOL_HOST": "invalid.invalid",
                "RIGOL_TIMEOUT": "0.1",
                "PYTHONPATH": str(Path(rigol_mcp.__file__).parent),
                "RIGOL_MANUAL_DIR": str(reference._reference_dir()),
            },
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                initialized = await session.initialize()
                self.assertIn("search_manual", initialized.instructions)
                for text in (
                    "Required one-time manual download",
                    "not bundled",
                    "python scripts/cache_manual.py",
                    "pdftotext",
                    "internet access",
                ):
                    self.assertIn(text, initialized.instructions[:512])
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertEqual(len(tools), 13)
                for name in ("search_manual", "get_manual"):
                    self.assertTrue(tools[name].annotations.readOnlyHint)
                    self.assertFalse(tools[name].annotations.openWorldHint)
                    self.assertIn("not bundled", tools[name].description)
                    self.assertIn("python scripts/cache_manual.py", tools[name].description)
                searched = await session.call_tool("search_manual", {"query": ":WAV:PRE?"})
                self.assertFalse(searched.isError)
                result = json.loads(searched.content[0].text)
                self.assertEqual(result["results"][0]["pdf_page"], 242)
                read = await session.call_tool("get_manual", {"page": 242})
                self.assertFalse(read.isError)
                self.assertIn("<xincrement>", read.content[0].text)
                resources = await session.list_resources()
                self.assertIn("rigol://manual/index", [str(r.uri) for r in resources.resources])
                templates = await session.list_resource_templates()
                self.assertIn(
                    "rigol://manual/page/{page}",
                    [r.uriTemplate for r in templates.resourceTemplates],
                )
                page = await session.read_resource("rigol://manual/page/242")
                self.assertIn("<xincrement>", page.contents[0].text)
                index = await session.read_resource("rigol://manual/index")
                self.assertIn("Command family", index.contents[0].text)
                invalid = await session.call_tool("get_manual", {"page": 0})
                self.assertTrue(invalid.isError)

        with tempfile.TemporaryDirectory() as directory:
            params.env["RIGOL_MANUAL_DIR"] = directory
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    for name, arguments in (
                        ("get_manual", {}),
                        ("search_manual", {"query": ":WAV:PRE?"}),
                    ):
                        result = await session.call_tool(name, arguments)
                        self.assertTrue(result.isError)
                        for text in (
                            "Required one-time manual download",
                            "not bundled",
                            "python scripts/cache_manual.py",
                            "pdftotext",
                            "internet access",
                        ):
                            self.assertIn(text, result.content[0].text)


if __name__ == "__main__":
    unittest.main()
