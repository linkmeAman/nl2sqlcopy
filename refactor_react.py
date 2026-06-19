import re

with open("nl2sql_service/react_agent.py", "r") as f:
    lines = f.readlines()

def get_block(start_line, end_line):
    return "".join(lines[start_line-1:end_line-1])

extract_block = get_block(713, 724)
looks_block = get_block(724, 734)
parse_block = get_block(836, 982)

with open("nl2sql_service/react_parser.py", "w") as f:
    f.write("import re\n")
    f.write("from nl2sql_service.models import ReActAction\n\n")
    f.write(extract_block)
    f.write("\n")
    f.write(looks_block)
    f.write("\n")
    f.write(parse_block)

# Remove these from react_agent.py and add import
new_lines = lines[:712] + lines[733:835] + lines[981:]
# Add import at the top
import_stmt = "from nl2sql_service.agent.react_parser import extract_think_block, looks_like_action_payload, parse_action\n"
new_lines.insert(32, import_stmt) # Insert roughly where other internal imports are

with open("nl2sql_service/react_agent.py", "w") as f:
    f.writelines(new_lines)

print("Parser refactored.")
