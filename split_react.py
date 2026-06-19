import re
import os

with open("nl2sql_service/react_agent.py", "r") as f:
    content = f.read()

# I will just extract the parser functions manually since they are simple
# extract_think_block(raw: str) -> tuple[str, str]
# looks_like_action_payload(raw: str) -> bool
# parse_action(answer: str) -> tuple[ReActAction, str]

