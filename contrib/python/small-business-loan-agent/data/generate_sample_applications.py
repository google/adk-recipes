# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Generate sample small business loan application PDFs for demo purposes.

All names, addresses, and data are entirely fictional.
Run: python data/generate_sample_applications.py
"""

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

OUTPUT_DIR = Path(__file__).parent / "sample_applications"


def _build_application_pdf(filename: str, data: dict) -> None:
    """Generate a single loan application summary PDF."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    filepath = OUTPUT_DIR / filename

    doc = SimpleDocTemplate(
        str(filepath),
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "Title2", parent=styles["Title"], fontSize=16, spaceAfter=6
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontSize=12,
        spaceAfter=8,
        spaceBefore=16,
    )

    elements = []

    # Header
    elements.append(
        Paragraph("SMALL BUSINESS LOAN APPLICATION SUMMARY", title_style)
    )

    # Business Information
    elements.append(Paragraph("Business Information", section_style))
    biz_table_data = [
        ["Business Name:", data["business_name"]],
        ["Business Type:", data["business_type"]],
        ["EIN:", data["ein"]],
        ["Industry:", data["industry"]],
        ["Years in Business:", data["years_in_business"]],
        ["Number of Employees:", data["num_employees"]],
    ]
    biz_table = Table(biz_table_data, colWidths=[2.5 * inch, 4 * inch])
    biz_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elements.append(biz_table)

    # Owner Information
    elements.append(Paragraph("Owner Information", section_style))
    owner_table_data = [
        ["Owner Name:", data["owner_name"]],
        ["Email:", data["owner_email"]],
        ["Phone:", data["owner_phone"]],
    ]
    owner_table = Table(owner_table_data, colWidths=[2.5 * inch, 4 * inch])
    owner_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(owner_table)

    # Business Address
    elements.append(Paragraph("Business Address", section_style))
    addr = data["address"]
    addr_table_data = [
        ["Street:", addr["street"]],
        ["City:", addr["city"]],
        ["State:", addr["state"]],
        ["ZIP Code:", addr["zip_code"]],
    ]
    addr_table = Table(addr_table_data, colWidths=[2.5 * inch, 4 * inch])
    addr_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(addr_table)

    # Financial Information
    elements.append(Paragraph("Financial Information", section_style))
    fin_table_data = [
        ["Annual Revenue:", data["annual_revenue"]],
        ["Net Profit:", data["net_profit"]],
        ["Existing Debt:", data["existing_debt"]],
    ]
    fin_table = Table(fin_table_data, colWidths=[2.5 * inch, 4 * inch])
    fin_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(fin_table)

    # Loan Details
    elements.append(Paragraph("Loan Request Details", section_style))
    loan_table_data = [
        ["Loan Amount Requested:", data["loan_amount"]],
        ["Loan Purpose:", data["loan_purpose"]],
        ["Requested Term:", data["loan_term"]],
        ["Collateral Offered:", data["collateral"]],
    ]
    loan_table = Table(loan_table_data, colWidths=[2.5 * inch, 4 * inch])
    loan_table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    elements.append(loan_table)

    # Footer
    elements.append(Spacer(1, 30))
    elements.append(
        Paragraph(
            "This is a sample document for demonstration purposes only. All data is fictional.",
            ParagraphStyle(
                "Footer",
                parent=styles["Normal"],
                fontSize=8,
                textColor=colors.grey,
            ),
        )
    )

    doc.build(elements)
    print(f"Generated: {filepath}")


def main():
    # Complete application — happy path
    _build_application_pdf(
        "sample_application_complete.pdf",
        {
            "business_name": "Coffee Roasters LLC",
            "business_type": "LLC",
            "ein": "00-1234567",
            "owner_name": "Jane Doe",
            "owner_email": "jane.doe@example.com",
            "owner_phone": "(555) 010-0100",
            "industry": "Food & Beverage",
            "years_in_business": "6",
            "num_employees": "12",
            "annual_revenue": "$850,000",
            "net_profit": "$120,000",
            "existing_debt": "None",
            "address": {
                "street": "742 Evergreen Terrace",
                "city": "Springfield",
                "state": "IL",
                "zip_code": "62704",
            },
            "loan_amount": "$150,000",
            "loan_purpose": "Equipment",
            "loan_term": "60 months",
            "collateral": "Commercial coffee roasting equipment",
        },
    )

    # Same application but with one missing field — triggers repair & resume
    _build_application_pdf(
        "sample_application_incomplete.pdf",
        {
            "business_name": "Coffee Roasters LLC",
            "business_type": "LLC",
            "ein": "00-1234567",
            "owner_name": "Jane Doe",
            "owner_email": "jane.doe@example.com",
            "owner_phone": "(555) 010-0100",
            "industry": "Food & Beverage",
            "years_in_business": "6",
            "num_employees": "12",
            "annual_revenue": "$850,000",
            "net_profit": "$120,000",
            "existing_debt": "None",
            "address": {
                "street": "742 Evergreen Terrace",
                "city": "Springfield",
                "state": "IL",
                "zip_code": "62704",
            },
            "loan_amount": "",  # Missing — triggers review
            "loan_purpose": "Equipment",
            "loan_term": "60 months",
            "collateral": "Commercial coffee roasting equipment",
        },
    )


if __name__ == "__main__":
    main()
