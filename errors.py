"""Expected, safe-to-display input errors."""

AUTHENTIC_DOCUMENT_MESSAGE = (
    "Please upload an authentic, text-based PDF or DOCX resume file. "
    "Scanned or image-based resumes are not supported."
)


class InvalidDocumentError(ValueError):
    pass


class InputLimitError(InvalidDocumentError):
    pass


INVALID_JD_MESSAGE = (
    "Please provide a valid job description containing the role's responsibilities, "
    "required skills, or qualifications."
)


class InvalidJobDescriptionError(ValueError):
    pass


class JDValidationUnavailableError(RuntimeError):
    pass
