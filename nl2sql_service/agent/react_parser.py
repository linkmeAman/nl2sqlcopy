import re
from nl2sql_service.models import ReActAction

def extract_think_block(raw: str) -> tuple[str, str]:
    think_start = raw.find("<think>")
    think_end = raw.find("</think>")
    if think_start != -1 and think_end != -1 and think_start < think_end:
        thought = raw[think_start + len("<think>") : think_end].strip()
        answer = raw[think_end + len("</think>") :].strip()
        return thought, answer

    return "", raw.strip()



def looks_like_action_payload(raw: str) -> bool:
    if re.search(r'"action"\s*:', raw, flags=re.IGNORECASE):
        return True
    if re.search(r"\b(?:ACTION|NEXT\s+ACTION)\b\s*[:=\-]", raw, flags=re.IGNORECASE):
        return True

    normalized = raw.upper().replace("-", "_").replace(" ", "_")
    return any(action.value in normalized for action in ReActAction)



def parse_action(answer: str) -> tuple[ReActAction, str]:
    def _normalize_token(raw: str) -> str:
        cleaned = raw.strip().strip("`*\"'[](){}.,;:")
        cleaned = cleaned.replace("-", "_").replace(" ", "_")
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", cleaned)
        return cleaned.upper()

    def _resolve_action(raw: str) -> ReActAction | None:
        normalized = _normalize_token(raw)
        if not normalized:
            return None

        exact = {action.value: action for action in ReActAction}
        if normalized in exact:
            return exact[normalized]

        aliases = {
            "RETRIEVE_PAST": ReActAction.RETRIEVE_PAST_CORRECTIONS,
            "LOAD_PAST_CORRECTIONS": ReActAction.RETRIEVE_PAST_CORRECTIONS,
            "RETRIEVE_SCHEMA": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "RETRIEVE_CONTEXT": ReActAction.RETRIEVE_MORE_CONTEXT,
            "RETRIEVE_MORE": ReActAction.RETRIEVE_MORE_CONTEXT,
            "FETCH_COLUMNS": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "GET_SCHEMA": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "FETCH_SCHEMA": ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
            "JOIN_PATHS": ReActAction.RETRIEVE_JOIN_PATHS,
            "RETRIEVE_JOINS": ReActAction.RETRIEVE_JOIN_PATHS,
            "SAMPLE_QUERIES": ReActAction.RETRIEVE_SAMPLE_QUERIES,
            "RETRIEVE_EXAMPLES": ReActAction.RETRIEVE_SAMPLE_QUERIES,
            "GENERATE": ReActAction.GENERATE_SQL,
            "WRITE_SQL": ReActAction.GENERATE_SQL,
            "VALIDATE": ReActAction.VALIDATE_AND_RETURN,
            "RETURN_SQL": ReActAction.VALIDATE_AND_RETURN,
            "ASK_CLARIFICATION": ReActAction.REQUEST_CLARIFICATION,
            "REQUEST_CLARIFICATION": ReActAction.REQUEST_CLARIFICATION,
            "CLARIFY": ReActAction.REQUEST_CLARIFICATION,
            "GIVEUP": ReActAction.GIVE_UP,
        }
        if normalized in aliases:
            return aliases[normalized]

        for action in ReActAction:
            if action.value in normalized:
                return action
        for alias, mapped_action in aliases.items():
            if alias in normalized:
                return mapped_action
        return None

    action_text: str | None = None
    action_input = ""

    action_line_pattern = re.compile(
        r"^(?:[-*]\s*)?(?:\*\*)?(?:NEXT\s+)?ACTION(?:\*\*)?\s*[:=\-]\s*(.+)$",
        re.IGNORECASE,
    )
    input_line_pattern = re.compile(
        r"^(?:[-*]\s*)?(?:\*\*)?(?:INPUT|ACTION_INPUT|INSTRUCTION)(?:\*\*)?\s*[:=\-]\s*(.+)$",
        re.IGNORECASE,
    )

    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        action_match = action_line_pattern.match(stripped)
        if action_match and not action_text:
            action_text = action_match.group(1).strip()
            continue

        input_match = input_line_pattern.match(stripped)
        if input_match and not action_input:
            action_input = input_match.group(1).strip()

    if not action_text:
        json_action_match = re.search(r'"action"\s*:\s*"([^"]+)"', answer, flags=re.IGNORECASE)
        if json_action_match:
            action_text = json_action_match.group(1).strip()

    if not action_input:
        json_input_match = re.search(
            r'"(?:input|action_input|instruction)"\s*:\s*"([^"]*)"',
            answer,
            flags=re.IGNORECASE,
        )
        if json_input_match:
            action_input = json_input_match.group(1).strip()

    if not action_text:
        for action in ReActAction:
            pattern = action.value.replace("_", r"[\s_-]*")
            if re.search(rf"\b{pattern}\b", answer, flags=re.IGNORECASE):
                action_text = action.value
                break

    if not action_text:
        natural_language_aliases = [
            (
                ReActAction.RETRIEVE_MORE_CONTEXT,
                r"\b(retrieve|search|find|load)\b.*\b(context|schema\s+group|more)\b",
            ),
            (
                ReActAction.RETRIEVE_SCHEMA_FOR_TABLES,
                r"\b(fetch|load|get|inspect)\b.*\b(schema|columns?)\b",
            ),
            (
                ReActAction.RETRIEVE_JOIN_PATHS,
                r"\b(join|relationship|path)\b",
            ),
            (
                ReActAction.RETRIEVE_SAMPLE_QUERIES,
                r"\b(sample|example|previous|similar)\b.*\b(query|pattern|sql)\b",
            ),
            (
                ReActAction.GENERATE_SQL,
                r"\b(generate|write|create|draft)\b.*\bsql\b",
            ),
            (
                ReActAction.VALIDATE_AND_RETURN,
                r"\b(validate|check)\b.*\b(return|sql|query)\b",
            ),
            (
                ReActAction.REQUEST_CLARIFICATION,
                r"\b(ask|request)\b.*\b(clarification|rephrase)\b",
            ),
            (
                ReActAction.GIVE_UP,
                r"\b(give\s*up|cannot|can't|unable|insufficient)\b",
            ),
        ]
        for action, pattern in natural_language_aliases:
            if re.search(pattern, answer, flags=re.IGNORECASE | re.DOTALL):
                action_text = action.value
                break

    if not action_text:
        return ReActAction.GIVE_UP, "Could not parse action"

    parsed_action = _resolve_action(action_text)
    if parsed_action is None:
        return ReActAction.GIVE_UP, "Could not parse action"

    return parsed_action, action_input


