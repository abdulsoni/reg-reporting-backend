from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet


BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "generated_reports"

OUTPUT_DIR.mkdir(exist_ok=True)

output_file = OUTPUT_DIR / "COREP_C90_30_JUN_2026.pdf"


def generate_pdf():

    document = SimpleDocTemplate(
        str(output_file),
        pagesize=A4,
    )

    styles = getSampleStyleSheet()

    story = []

    story.append(
        Paragraph(
            "COREP C90 Report",
            styles["Title"],
        )
    )

    story.append(
        Paragraph(
            "J P Morgan Bank",
            styles["Heading2"],
        )
    )

    story.append(
        Paragraph(
            "Date: 30 Jun 2026",
            styles["Normal"],
        )
    )

    story.append(
        Paragraph(
            "Delivered to: Prudential Regulatory Authority",
            styles["Normal"],
        )
    )

    story.append(Spacer(1, 20))

    story.append(
        Paragraph(
            "Regulatory Data Point",
            styles["Heading2"],
        )
    )

    data = [
        [
            "report_code",
            "row_code",
            "column_code",
            "attribute_name",
            "reported_value_usd",
        ],
        [
            "C90",
            "0010",
            "0200",
            "exposure_value",
            "5093.57",
        ],
    ]

    table = Table(data)

    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 1, colors.black),
                ("ALIGN", (4, 1), (4, -1), "RIGHT"),
                ("PADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )

    story.append(table)

    story.append(Spacer(1, 30))

    story.append(
        Paragraph(
            "Selected datapoint: C90 / r0010 / c0200",
            styles["Normal"],
        )
    )

    story.append(
        Paragraph(
            "Metric: Exposure Value",
            styles["Normal"],
        )
    )

    story.append(
        Paragraph(
            "Reported Value: 5093.57 USD",
            styles["Normal"],
        )
    )

    document.build(story)

    print(f"PDF generated: {output_file}")


if __name__ == "__main__":
    generate_pdf()