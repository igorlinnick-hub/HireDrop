import os

TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "templates", "cover_letter.txt")


def generate_cover_letter(job):
    try:
        with open(TEMPLATE_PATH, "r") as f:
            template = f.read()
    except FileNotFoundError:
        print("[cover_letter] Template file not found")
        return ""

    letter = template.replace("{company}", job.get("company", ""))
    letter = letter.replace("{title}", job.get("title", ""))
    return letter
