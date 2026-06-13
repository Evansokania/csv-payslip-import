"""Export Cursor agent chat transcript to PDF."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from fpdf import FPDF

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANSCRIPT = Path(
    r"C:\Users\Admin\.cursor\projects\c-laragon-www-Hr-Genie-Payroll\agent-transcripts"
    r"\8b9929cf-c930-47ee-9fda-c0eba74396dc"
    r"\8b9929cf-c930-47ee-9fda-c0eba74396dc.jsonl"
)
OUTPUT = ROOT / "exports" / "csv-payslip-import-chat-export.pdf"


def _strip_user_query(text: str) -> str:
    text = re.sub(r"</?user_query>", "", text).strip()
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def _clean_assistant_text(text: str) -> str:
    if text.strip() == "[REDACTED]":
        return ""
    text = re.sub(r"```[\w]*\n?", "```\n", text)
    return text.strip()


def _load_messages(transcript_path: Path) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    with transcript_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = row.get("role")
            content = row.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue

            parts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "text":
                    continue
                txt = block.get("text") or ""
                if role == "user":
                    txt = _strip_user_query(txt)
                elif role == "assistant":
                    txt = _clean_assistant_text(txt)
                if txt:
                    parts.append(txt)

            if not parts:
                continue
            body = "\n\n".join(parts)
            key = (role, body[:200])
            if key in seen:
                continue
            seen.add(key)
            messages.append((role, body))

    return messages


class ChatPDF(FPDF):
    def __init__(self) -> None:
        super().__init__()
        self.set_auto_page_break(auto=True, margin=18)
        font = Path(r"C:\Windows\Fonts\arial.ttf")
        if font.exists():
            self.add_font("Arial", "", str(font))
            self.add_font("Arial", "B", str(Path(r"C:\Windows\Fonts\arialbd.ttf")))
            self.body_font = ("Arial", "", 10)
            self.title_font = ("Arial", "B", 14)
            self.role_font = ("Arial", "B", 11)
        else:
            self.body_font = ("Helvetica", "", 10)
            self.title_font = ("Helvetica", "B", 14)
            self.role_font = ("Helvetica", "B", 11)

    def header(self) -> None:
        self.set_font(*self.title_font)
        self.cell(0, 10, "CSV Payslip Import — Chat Export", new_x="LMARGIN", new_y="NEXT")
        self.set_font(*self.body_font)
        self.set_text_color(90, 90, 90)
        self.cell(0, 6, f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font(*self.body_font)
        self.set_text_color(120, 120, 120)
        self.cell(0, 8, f"Page {self.page_no()}/{{nb}}", align="C")

    def write_block(self, role: str, text: str) -> None:
        label = "User" if role == "user" else "Assistant"
        color = (0, 70, 140) if role == "user" else (20, 100, 60)
        self.set_font(*self.role_font)
        self.set_text_color(*color)
        self.cell(0, 8, label, new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(0, 0, 0)
        self.set_font(*self.body_font)
        self.multi_cell(0, 5.5, text)
        self.ln(4)


def main() -> int:
    transcript = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRANSCRIPT
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else OUTPUT

    if not transcript.exists():
        print(f"Transcript not found: {transcript}")
        return 1

    messages = _load_messages(transcript)
    if not messages:
        print("No messages found in transcript.")
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)

    pdf = ChatPDF()
    pdf.alias_nb_pages()
    pdf.add_page()

    pdf.set_font(*pdf.body_font)
    pdf.multi_cell(
        0,
        5.5,
        (
            "This document exports the Cursor chat about csv-payslip-import and "
            "Hr-Genie-Payroll payroll alignment. Tool-call details are omitted where redacted."
        ),
    )
    pdf.ln(6)

    for role, text in messages:
        pdf.write_block(role, text)

    # Append export confirmation if not already in transcript.
    export_note = (
        "Created a PDF export of this chat conversation.\n\n"
        f"Output file: {output}\n\n"
        "To regenerate the PDF anytime:\n"
        "  cd C:\\laragon\\www\\csv-payslip-import\n"
        "  .\\.venv\\Scripts\\python.exe scripts\\export_chat_to_pdf.py\n\n"
        "Note: Internal tool-call steps are omitted where the transcript marks them as [REDACTED]. "
        "All user questions and assistant answers are included."
    )
    if not any(role == "assistant" and "PDF export" in text for role, text in messages):
        pdf.write_block("assistant", export_note)

    pdf.output(str(output))
    print(f"PDF written to: {output}")
    print(f"Messages exported: {len(messages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
