import pytest
from src.handlers.form_detector import FormDetector, FieldType, FieldCategory

def test_classify_field_mapping():
    detector = FormDetector()
    
    # Test cases mapping input labels to expected categories
    test_cases = [
        ("First Name", FieldType.TEXT_INPUT, FieldCategory.FIRST_NAME),
        ("Surname", FieldType.TEXT_INPUT, FieldCategory.LAST_NAME),
        ("Last Name", FieldType.TEXT_INPUT, FieldCategory.LAST_NAME),
        ("Your full name", FieldType.TEXT_INPUT, FieldCategory.FULL_NAME),
        ("Email address", FieldType.TEXT_INPUT, FieldCategory.EMAIL),
        ("e-mail", FieldType.TEXT_INPUT, FieldCategory.EMAIL),
        ("Phone number", FieldType.TEXT_INPUT, FieldCategory.PHONE),
        ("mobile", FieldType.TEXT_INPUT, FieldCategory.PHONE),
        ("Upload Resume (PDF, DOCX)", FieldType.FILE_UPLOAD, FieldCategory.RESUME),
        ("CV", FieldType.FILE_UPLOAD, FieldCategory.RESUME),
        ("Cover Letter", FieldType.FILE_UPLOAD, FieldCategory.COVER_LETTER),
        ("How many years of Python experience do you have?", FieldType.TEXT_INPUT, FieldCategory.YEARS_EXPERIENCE),
        ("Highest education level", FieldType.SELECT, FieldCategory.EDUCATION),
        ("Degree", FieldType.SELECT, FieldCategory.EDUCATION),
        ("Desired salary", FieldType.TEXT_INPUT, FieldCategory.SALARY_EXPECTATION),
        ("Expected salary", FieldType.NUMBER, FieldCategory.SALARY_EXPECTATION),
        ("When are you available to start?", FieldType.DATE, FieldCategory.START_DATE),
        ("Are you authorized to work in the United States?", FieldType.RADIO, FieldCategory.WORK_AUTHORIZATION),
        ("Will you now or in the future require visa sponsorship?", FieldType.RADIO, FieldCategory.WORK_AUTHORIZATION),
        ("Are you willing to relocate?", FieldType.RADIO, FieldCategory.WILLING_TO_RELOCATE),
        ("LinkedIn profile URL", FieldType.TEXT_INPUT, FieldCategory.LINKEDIN),
        ("Portfolio website", FieldType.TEXT_INPUT, FieldCategory.WEBSITE),
        ("github link", FieldType.TEXT_INPUT, FieldCategory.WEBSITE),
        ("City", FieldType.TEXT_INPUT, FieldCategory.CITY),
        ("State/Province", FieldType.TEXT_INPUT, FieldCategory.STATE),
        ("Zip Code", FieldType.TEXT_INPUT, FieldCategory.ZIP_CODE),
        ("Street Address", FieldType.TEXT_INPUT, FieldCategory.ADDRESS),
        ("What is your current location?", FieldType.TEXT_INPUT, FieldCategory.LOCATION),
    ]
    
    for label, field_type, expected_category in test_cases:
        category = detector._classify_field(label, field_type)
        assert category == expected_category, f"Failed for label='{label}' and type={field_type}. Expected {expected_category}, got {category}"

def test_classify_field_fallback():
    detector = FormDetector()
    
    # Generic question with no keywords should map to SCREENING_QUESTION for text inputs
    assert detector._classify_field("What is your favorite color?", FieldType.TEXT_INPUT) == FieldCategory.SCREENING_QUESTION
    assert detector._classify_field("Please describe a time you failed.", FieldType.TEXTAREA) == FieldCategory.SCREENING_QUESTION
    
    # Non-text elements with no keyword matches should map to UNKNOWN
    assert detector._classify_field("Custom Option Select", FieldType.SELECT) == FieldCategory.UNKNOWN
    assert detector._classify_field("Custom Radio Check", FieldType.RADIO) == FieldCategory.UNKNOWN
    
    # Empty labels should map to UNKNOWN
    assert detector._classify_field("", FieldType.TEXT_INPUT) == FieldCategory.UNKNOWN
