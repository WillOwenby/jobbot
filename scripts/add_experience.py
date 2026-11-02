"""One-off: point config.yaml at experience.txt and update the years-of-experience answers.

Run from the jobbot folder:  python scripts/add_experience.py
A backup is saved as config.backup.yaml first.
"""
import sys
sys.path.insert(0, ".")
import re, pathlib, shutil
p = pathlib.Path("config.yaml")
if not p.exists():
    sys.exit("Run this from the jobbot folder (where config.yaml is).")
if not pathlib.Path("experience.txt").exists():
    sys.exit("experience.txt not found. Create it first: notepad experience.txt")
shutil.copy(p, "config.backup.yaml")
s = p.read_text(encoding="utf-8")
if "experience_file:" not in s:
    s, n = re.subn(r"(?m)^(  cover_letter_example:.*\n)", r"\1  experience_file: experience.txt\n", s, count=1)
    print("experience_file line added" if n else "!! could not find cover_letter_example; add experience_file by hand")
else:
    print("experience_file already present")
s, a = re.subn(r'(match: "years\.\*epic\|epic\.\*years", answer: )"\d+"', r'\1"10"', s)
s, b = re.subn(r'(match: "years\.\*\(healthcare\|health care\)", answer: )"\d+"', r'\1"10"', s)
s, c = re.subn(r'(match: "\^\(how many \)\?\(total \)\?years of [^"]*", answer: )"\d+"', r'\1"19"', s)
print(f"Epic years -> 10: {'done' if a else 'NOT FOUND'}; healthcare years -> 10: {'done' if b else 'NOT FOUND'}; "
      f"total work years -> 19: {'done' if c else 'NOT FOUND'}")
p.write_text(s, encoding="utf-8")
from jobbot.config import load_config
cfg = load_config("config.yaml")
print("config loads OK; experience file:", cfg.ai.experience_file)
from jobbot.forms import AnswerBook
book = AnswerBook(cfg.profile, cfg.answers)
for q in ["How many years of Epic experience do you have?", "How many years of healthcare experience do you have?",
          "How many years of work experience do you have?"]:
    print(f"  {q} -> {book.lookup(q)}")
