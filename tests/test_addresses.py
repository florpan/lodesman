# SPDX-License-Identifier: MIT
"""
Symbol addresses: how they parse, what they match, and how they print.

The resolver's rules, from workdocs/ADDRESSING.md: an address means exactly one
declaration or the call fails; qualifiers match the end of the enclosing chain;
a match nested in another match is dropped (a class hides its same-named
constructor); `#n` picks an overload by order and `(types)` by parameters; and
the printed form is the shortest that is unique within the file.
"""

from __future__ import annotations

import unittest

from lodesman.server import (
    Declaration,
    ToolError,
    address_in_file,
    innermost,
    match_declarations,
    parameters_of,
    parse_address,
)


def span(start_line: int, start_char: int, end_line: int, end_char: int) -> dict:
    return {"start": {"line": start_line, "character": start_char},
            "end": {"line": end_line, "character": end_char}}


def outline() -> list[Declaration]:
    """
    namespace App
      class Server                     L1-20
        Server(RequestDelegate)        L2     constructor
        GetUser(string)                L4-6
        GetUser(int)                   L8-10
        Name                           L12
      class Client                     L22-30
        Name                           L24
    """
    def decl(name, kind, rng, chain, parent=None, detail=None):
        symbol = {"name": name, "kind": kind, "range": rng, "selectionRange": rng, "parent": parent}
        if detail:
            symbol["detail"] = detail
        return Declaration(symbol, chain)

    ns = decl("App", 3, span(0, 0, 31, 0), ["App"])
    server = decl("Server", 5, span(1, 0, 20, 1), ["App", "Server"], ns.symbol)
    client = decl("Client", 5, span(22, 0, 30, 1), ["App", "Client"], ns.symbol)
    return [
        ns, server,
        decl("Server(RequestDelegate)", 9, span(2, 4, 2, 40), ["App", "Server", "Server"], server.symbol),
        decl("GetUser(string)", 6, span(4, 4, 6, 5), ["App", "Server", "GetUser"], server.symbol),
        decl("GetUser(int)", 6, span(8, 4, 10, 5), ["App", "Server", "GetUser"], server.symbol),
        decl("Name", 7, span(12, 4, 12, 30), ["App", "Server", "Name"], server.symbol, "Name : string"),
        client,
        decl("Name", 7, span(24, 4, 24, 30), ["App", "Client", "Name"], client.symbol),
    ]


class TestParse(unittest.TestCase):
    def test_forms(self):
        cases = {
            "GetUser": (None, ("GetUser",), None, None, None),
            "Server.GetUser": (None, ("Server", "GetUser"), None, None, None),
            "backend/Server.cs:Server.GetUser#2": ("backend/Server.cs", ("Server", "GetUser"), "#2", None, None),
            "backend/Server.cs:GetUser(int, string)": ("backend/Server.cs", ("GetUser",), "(int,string)", None, None),
            "backend/*:RequestLoggingMiddleware.RequestLoggingMiddleware":
                ("backend/*", ("RequestLoggingMiddleware", "RequestLoggingMiddleware"), None, None, None),
            "backend/Server.cs:42": ("backend/Server.cs", (), None, 42, None),
            "backend/Server.cs:42:17": ("backend/Server.cs", (), None, 42, 17),
            "src/lib.rs:Store::get": ("src/lib.rs", ("Store", "get"), None, None, None),
            "Store::get": (None, ("Store", "get"), None, None, None),
        }
        for text, expected in cases.items():
            with self.subTest(address=text):
                a = parse_address(text)
                self.assertEqual((a.where, a.path, a.overload, a.line, a.column), expected)

    def test_a_line_needs_a_file(self):
        with self.assertRaises(ToolError):
            parse_address("42")

    def test_empty_is_refused(self):
        with self.assertRaises(ToolError):
            parse_address("  ")


class TestMatch(unittest.TestCase):
    def names(self, hits):
        return [h.symbol["name"] for h in hits]

    def test_a_class_hides_its_constructor(self):
        self.assertEqual(self.names(match_declarations(outline(), ["Server"])), ["Server"])
        self.assertEqual(self.names(match_declarations(outline(), ["Server", "Server"])),
                         ["Server(RequestDelegate)"])

    def test_qualifiers_match_the_end_of_the_chain(self):
        self.assertEqual(len(match_declarations(outline(), ["Name"])), 2)
        self.assertEqual(self.names(match_declarations(outline(), ["Client", "Name"])), ["Name"])
        self.assertEqual(len(match_declarations(outline(), ["App", "Server", "Name"])), 1)
        self.assertEqual(match_declarations(outline(), ["Other", "Name"]), [])

    def test_overloads_by_order_and_by_parameters(self):
        self.assertEqual(self.names(match_declarations(outline(), ["GetUser"], "#2")), ["GetUser(int)"])
        self.assertEqual(self.names(match_declarations(outline(), ["GetUser"], "(string)")),
                         ["GetUser(string)"])
        self.assertEqual(match_declarations(outline(), ["GetUser"], "#3"), [])

    def test_parameters_come_from_name_or_detail(self):
        self.assertEqual(parameters_of({"name": "Get(string key, int n)"}), "(stringkey,intn)")
        self.assertEqual(parameters_of({"name": "get", "detail": "get(String) : Record"}), "(String)")
        self.assertIsNone(parameters_of({"name": "Name", "detail": "Name : string"}))


class TestPrint(unittest.TestCase):
    def test_the_shortest_unique_path(self):
        decls = outline()
        by_name = {(d.symbol["name"], d.symbol["range"]["start"]["line"]): d for d in decls}
        self.assertEqual(address_in_file(by_name[("Server", 1)], decls), "Server")
        self.assertEqual(address_in_file(by_name[("Server(RequestDelegate)", 2)], decls), "Server.Server")
        self.assertEqual(address_in_file(by_name[("Name", 12)], decls), "Server.Name")
        self.assertEqual(address_in_file(by_name[("GetUser(string)", 4)], decls), "GetUser#1")
        self.assertEqual(address_in_file(by_name[("GetUser(int)", 8)], decls), "GetUser#2")

    def test_every_printed_address_resolves_to_itself(self):
        # The property that matters: what is printed, fed back, means the same.
        decls = outline()
        for declaration in decls:
            printed = parse_address("f.cs:" + address_in_file(declaration, decls))
            with self.subTest(printed=printed.text):
                hits = match_declarations(decls, printed.path, printed.overload)
                self.assertEqual(len(hits), 1)
                self.assertIs(hits[0], declaration)

    def test_innermost_declaration_at_a_line(self):
        decls = outline()
        self.assertEqual(innermost(decls, 5).symbol["name"], "GetUser(string)")
        self.assertEqual(innermost(decls, 15).symbol["name"], "Server")
        self.assertEqual(innermost(decls, 21).symbol["name"], "App")


if __name__ == "__main__":
    unittest.main()
