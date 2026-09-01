"""One-off script to generate a sample syllabus PDF for testing."""

from pathlib import Path


def create_sample_pdf(output_path: Path) -> None:
  lines = [
    "CS 101 - Introduction to Computer Science",
    "Fall 2026 Syllabus",
    "Instructor: Dr. Smith",
    "",
    "MAJOR ASSIGNMENTS AND EXAMS",
    "",
    "Lab Report 1 - Due September 2, 2026",
    "Quiz 2 - Due September 5, 2026 at 11:59 PM",
    "Midterm Exam - October 15, 2026 at 2:00 PM",
    "Final Project - November 20, 2026",
    "Final Exam - December 10, 2026 at 9:00 AM",
    "",
    "MATH 201 - Calculus II",
    "Fall 2026",
    "",
    "Homework 3 - Due September 4, 2026",
    "Midterm 1 - October 22, 2026",
    "Final Exam - December 12, 2026",
  ]

  y = 750
  content_parts = ["BT /F1 11 Tf"]
  for line in lines:
    safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content_parts.append(f"50 {y} Td ({safe}) Tj")
    content_parts.append("0 -16 Td")
    y -= 16
  content_parts.append("ET")
  stream = "\n".join(content_parts)
  stream_bytes = stream.encode("latin-1", errors="replace")

  objects = []
  objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
  objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
  objects.append(
    b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
    b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n"
  )
  objects.append(
    f"4 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode()
    + stream_bytes
    + b"\nendstream\nendobj\n"
  )
  objects.append(
    b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
  )

  pdf = bytearray(b"%PDF-1.4\n")
  offsets = [0]
  for obj in objects:
    offsets.append(len(pdf))
    pdf.extend(obj)

  xref_pos = len(pdf)
  pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
  pdf.extend(b"0000000000 65535 f \n")
  for offset in offsets[1:]:
    pdf.extend(f"{offset:010d} 00000 n \n".encode())

  pdf.extend(
    f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
    f"startxref\n{xref_pos}\n%%EOF\n".encode()
  )

  output_path.parent.mkdir(exist_ok=True)
  output_path.write_bytes(pdf)
  print(f"Created {output_path.resolve()}")


if __name__ == "__main__":
  create_sample_pdf(Path("syllabi") / "sample_cs101_syllabus.pdf")
