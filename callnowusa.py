import json
import logging
import sqlite3
import re
import time
from openai import AzureOpenAI
from callnowusa import Client as CallNowUSAClient
from datetime import datetime, timedelta
import threading
from dateutil.parser import parse as parse_datetime
import pytz

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Configuration ---
CALLNOWUSA_NUMBER = 'default'
ACCOUNT_SID = 'SID_d5cf1823-5664-42cc-b6b6-fb10bcdaec56'
AUTH_TOKEN = 'AUTH_aaa784bc-a599-499f-946b-ba7115c59726'
AZURE_API_KEY = '1JlJLWNSeUNZXD4QnrDuXPHS34Lj8xgowfo9jQp9K1Kz7LNvj6b7JQQJ99BFACYeBjFXJ3w3AAABACOGQOIQ'
AZURE_ENDPOINT = 'https://callnowusa.openai.azure.com/'
TIMEZONE = 'US/Eastern'

# --- Initialize Clients ---
openai_client = AzureOpenAI(
    api_key=AZURE_API_KEY,
    azure_endpoint=AZURE_ENDPOINT,
    api_version="2024-02-15-preview"
)

callnow_client = CallNowUSAClient(
    account_sid=ACCOUNT_SID,
    auth_token=AUTH_TOKEN,
    phone_number=CALLNOWUSA_NUMBER
)

# --- Thread Lock for Global Variables ---
cache_lock = threading.Lock()

# --- Store Last Forwarding Command and Pending Contact Prompt ---
last_forwarding = {"from_number": None, "to_contact": None, "to_number": None}
pending_contact_prompt = None
last_inbox_check = None
inbox_cache = None

# --- SQLite Contact and Rule DB ---
def init_db():
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS contacts (
                     alias TEXT PRIMARY KEY,
                     phone_number TEXT
                 )''')
        c.execute('''CREATE TABLE IF NOT EXISTS rules (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     from_contact TEXT,
                     from_number TEXT,
                     action_type TEXT,
                     reply_message TEXT,
                     forward_to_contact TEXT,
                     forward_to_number TEXT,
                     condition TEXT,
                     start_time TEXT,
                     end_time TEXT,
                     timeout INTEGER,
                     is_one_time INTEGER,
                     created_at TEXT
                 )''')
        c.execute('''CREATE TABLE IF NOT EXISTS message_log (
                     id INTEGER PRIMARY KEY AUTOINCREMENT,
                     sender TEXT,
                     message TEXT,
                     timestamp TEXT,
                     replied INTEGER DEFAULT 0
                 )''')
        conn.commit()

def add_contact(alias, phone_number):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('REPLACE INTO contacts (alias, phone_number) VALUES (?, ?)', (alias.lower(), phone_number))
        conn.commit()

def resolve_contact(alias):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('SELECT phone_number FROM contacts WHERE alias = ?', (alias.lower(),))
        result = c.fetchone()
        return result[0] if result else None

def list_contacts():
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('SELECT alias, phone_number FROM contacts')
        rows = c.fetchall()
        return rows

def delete_contact(alias):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM contacts WHERE alias = ?', (alias.lower(),))
        conn.commit()

def add_rule(from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        created_at = datetime.now(pytz.timezone(TIMEZONE)).isoformat()
        c.execute('INSERT INTO rules (from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                  (from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, created_at))
        rule_id = c.lastrowid
        conn.commit()
    if action_type == "forward" and not timeout:
        callnow_client.sms_forward(
            to_number=from_number,
            to_number2=forward_to_number,
            from_=CALLNOWUSA_NUMBER
        )
    elif action_type == "reply" or timeout:
        check_existing_messages_for_rule(from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, rule_id)
    return rule_id

def delete_rule(rule_id=None, from_number=None, to_contact=None):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        if rule_id:
            c.execute('SELECT from_number, forward_to_number, action_type FROM rules WHERE id = ?', (rule_id,))
            rule = c.fetchone()
            if rule and rule[2] == "forward" and rule[0] and rule[1]:
                callnow_client.sms_forward_stop(
                    to_number=rule[0],
                    to_number2=rule[1],
                    from_=CALLNOWUSA_NUMBER
                )
            c.execute('DELETE FROM rules WHERE id = ?', (rule_id,))
        elif from_number and to_contact:
            c.execute('SELECT from_number, forward_to_number, action_type FROM rules WHERE from_number = ? AND forward_to_contact = ?', (from_number, to_contact))
            rule = c.fetchone()
            if rule and rule[2] == "forward" and rule[0] and rule[1]:
                callnow_client.sms_forward_stop(
                    to_number=rule[0],
                    to_number2=rule[1],
                    from_=CALLNOWUSA_NUMBER
                )
            c.execute('DELETE FROM rules WHERE from_number = ? AND forward_to_contact = ?', (from_number, to_contact))
        else:
            c.execute('SELECT id, from_number, forward_to_number, action_type FROM rules WHERE action_type = "forward"')
            for rule_id, from_number, forward_to_number, _ in c.fetchall():
                if from_number and forward_to_number:
                    callnow_client.sms_forward_stop(
                        to_number=from_number,
                        to_number2=forward_to_number,
                        from_=CALLNOWUSA_NUMBER
                    )
            c.execute('DELETE FROM rules')
        conn.commit()

def list_rules(forward_only=False):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('SELECT id, from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, created_at FROM rules')
        rows = c.fetchall()
        if forward_only:
            rows = [row for row in rows if row[3] == "forward"]
        return rows

def log_message(sender, message, timestamp):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('INSERT INTO message_log (sender, message, timestamp, replied) VALUES (?, ?, ?, 0)', (sender, message, timestamp))
        conn.commit()

def mark_message_replied(sender, timestamp):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('UPDATE message_log SET replied = 1 WHERE sender = ? AND timestamp = ?', (sender, timestamp))
        conn.commit()

def check_message_replied(sender, timestamp):
    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute('SELECT replied FROM message_log WHERE sender = ? AND timestamp = ?', (sender, timestamp))
        result = c.fetchone()
        return result[0] if result else False

def is_valid_number(number):
    return bool(number and re.fullmatch(r'\+\d{10,15}', number))

def is_valid_body(text):
    return bool(text and text.strip())

def is_valid_time_range(start_time, end_time):
    try:
        datetime.strptime(start_time, '%H:%M')
        datetime.strptime(end_time, '%H:%M')
        return True
    except ValueError:
        return False

def check_existing_messages_for_rule(from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, rule_id):
    global inbox_cache, last_inbox_check
    with cache_lock:
        if not last_inbox_check or (datetime.now(pytz.timezone(TIMEZONE)) - last_inbox_check) > timedelta(seconds=5):
            try:
                inbox_message = callnow_client.check_inbox(from_=CALLNOWUSA_NUMBER)
                response = inbox_message.fetch()
                status_text = response.get("status", "")
                inbox_cache = parse_inbox_messages(status_text)
                last_inbox_check = datetime.now(pytz.timezone(TIMEZONE))
                logger.info(f"Inbox checked, {len(inbox_cache)} messages fetched")
            except Exception as e:
                logger.error(f"Failed to fetch inbox: {e}")
                return
    messages = inbox_cache or []
    rule_number = from_number or resolve_contact(from_contact) if from_contact else None
    if not rule_number:
        logger.info(f"No valid rule number for from_contact={from_contact}, from_number={from_number}")
        return
    cutoff_time = datetime.now(pytz.timezone(TIMEZONE)) - timedelta(seconds=max(60, timeout or 60))
    for sender, text, timestamp, direction in messages:
        msg_time = parse_datetime(timestamp)
        if sender == rule_number and direction == "received" and msg_time >= cutoff_time and not check_message_replied(sender, timestamp):
            logger.info(f"Processing message from {sender} at {timestamp}: {text}")
            if condition:
                try:
                    response = openai_client.chat.completions.create(
                        model="gpt-4o",
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    f"Does this SMS message contain the exact phrase '{condition}'? Return JSON: {{\"matches\": true}} or {{\"matches\": false}}."
                                )
                            },
                            {"role": "user", "content": f"Message: {text}"}
                        ],
                        temperature=0,
                    )
                    content = response.choices[0].message.content.strip().replace("'", '"')
                    result = json.loads(content)
                    if not result.get("matches", False):
                        logger.info(f"Message does not match condition '{condition}'")
                        continue
                except Exception as e:
                    logger.error(f"Failed to process condition check: {e}")
                    continue
            if start_time and end_time:
                msg_hour = msg_time.strftime('%H:%M')
                if not (start_time <= msg_hour <= end_time):
                    logger.info(f"Message outside time range {start_time}-{end_time}")
                    continue
            if action_type == "reply" and reply_message:
                if timeout:
                    logger.info(f"Scheduling reply '{reply_message}' to {sender} after {timeout} seconds")
                    threading.Timer(timeout, send_reply, args=(sender, reply_message, timestamp, from_contact, rule_id, is_one_time)).start()
                else:
                    send_reply(sender, reply_message, timestamp, from_contact, rule_id, is_one_time)
            elif action_type == "forward" and forward_to_number and timeout:
                logger.info(f"Scheduling forward to {forward_to_number} after {timeout} seconds")
                threading.Timer(timeout, check_forward_timeout, args=(sender, text, timestamp, forward_to_number, rule_id, from_contact)).start()

def send_reply(sender, reply_message, timestamp, from_contact, rule_id, is_one_time):
    if not check_message_replied(sender, timestamp):
        try:
            resp = callnow_client.messages.create(
                to=sender,
                from_=CALLNOWUSA_NUMBER,
                body=reply_message
            )
            print(f"✅ Sent reply '{reply_message}' to {from_contact or sender} at {datetime.now(pytz.timezone(TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S')}")
            mark_message_replied(sender, timestamp)
            if is_one_time:
                delete_rule(rule_id=rule_id)
                print(f"🗑️ One-time rule for {from_contact or sender} has been removed.")
        except Exception as e:
            logger.error(f"Failed to send reply: {e}")

def monitor_inbox():
    global inbox_cache, last_inbox_check
    while True:
        with cache_lock:
            if not last_inbox_check or (datetime.now(pytz.timezone(TIMEZONE)) - last_inbox_check) > timedelta(seconds=5):
                try:
                    inbox_message = callnow_client.check_inbox(from_=CALLNOWUSA_NUMBER)
                    response = inbox_message.fetch()
                    status_text = response.get("status", "")
                    messages = parse_inbox_messages(status_text)
                    inbox_cache = messages
                    last_inbox_check = datetime.now(pytz.timezone(TIMEZONE))
                    logger.info(f"Background inbox check, {len(messages)} messages fetched")
                except Exception as e:
                    logger.error(f"Failed to fetch inbox in monitor: {e}")
            else:
                messages = inbox_cache or []
        rules = list_rules()
        for sender, text, timestamp, direction in messages:
            if check_message_replied(sender, timestamp):
                continue
            for rule_id, from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, _ in rules:
                rule_number = from_number or resolve_contact(from_contact) if from_contact else None
                if sender == rule_number and direction == "received":
                    msg_time = parse_datetime(timestamp)
                    cutoff_time = datetime.now(pytz.timezone(TIMEZONE)) - timedelta(seconds=max(60, timeout or 60))
                    if msg_time < cutoff_time:
                        logger.info(f"Message from {sender} at {timestamp} is too old")
                        continue
                    if condition:
                        try:
                            response = openai_client.chat.completions.create(
                                model="gpt-4o",
                                messages=[
                                    {
                                        "role": "system",
                                        "content": (
                                            f"Does this SMS message contain the exact phrase '{condition}'? Return JSON: {{\"matches\": true}} or {{\"matches\": false}}."
                                        )
                                    },
                                    {"role": "user", "content": f"Message: {text}"}
                                ],
                                temperature=0,
                            )
                            content = response.choices[0].message.content.strip().replace("'", '"')
                            result = json.loads(content)
                            if not result.get("matches", False):
                                logger.info(f"Message does not match condition '{condition}'")
                                continue
                        except Exception as e:
                            logger.error(f"Failed to process condition check in monitor: {e}")
                            continue
                    if start_time and end_time:
                        msg_hour = msg_time.strftime('%H:%M')
                        if not (start_time <= msg_hour <= end_time):
                            logger.info(f"Message outside time range {start_time}-{end_time}")
                            continue
                    if action_type == "reply" and reply_message:
                        if timeout:
                            logger.info(f"Scheduling reply '{reply_message}' to {sender} after {timeout} seconds")
                            threading.Timer(timeout, send_reply, args=(sender, reply_message, timestamp, from_contact, rule_id, is_one_time)).start()
                        else:
                            send_reply(sender, reply_message, timestamp, from_contact, rule_id, is_one_time)
                    elif action_type == "forward" and forward_to_number and timeout:
                        logger.info(f"Scheduling forward to {forward_to_number} after {timeout} seconds")
                        threading.Timer(timeout, check_forward_timeout, args=(sender, text, timestamp, forward_to_number, rule_id, from_contact)).start()
        time.sleep(5)

def check_forward_timeout(sender, text, timestamp, forward_to_number, rule_id, from_contact):
    if not check_message_replied(sender, timestamp):
        try:
            resp = callnow_client.messages.create(
                to=forward_to_number,
                from_=CALLNOWUSA_NUMBER,
                body=f"Forwarded from {sender}: {text}"
            )
            print(f"✅ Forwarded message from {from_contact or sender} to {forward_to_number} at {datetime.now(pytz.timezone(TIMEZONE)).strftime('%Y-%m-%d %H:%M:%S')}")
            mark_message_replied(sender, timestamp)
        except Exception as e:
            logger.error(f"Failed to forward message: {e}")

def parse_inbox_messages(status_text):
    message_blocks = re.split(r'\],\s*\[', status_text.strip('[]'))
    messages = []
    for block in message_blocks:
        block = f"[{block}]" if not block.startswith('[') else block
        block = f"{block}" if not block.endswith(']') else block
        match = re.match(r'\[\s*(\+\d+)\s*:\s*"(.*?)"\s*(sent|received)\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*\]', block)
        if match:
            sender, text, direction, timestamp = match.groups()
            text = text.strip()
            if is_valid_number(sender) and text.strip():
                parsed_time = parse_datetime(timestamp)
                parsed_time = parsed_time.replace(tzinfo=pytz.timezone(TIMEZONE)) if not parsed_time.tzinfo else parsed_time
                messages.append((sender, text, parsed_time.isoformat(), direction))
    return messages

def parse_time_limit(time_limit_str):
    """Convert a time limit string (e.g., '20 seconds') to seconds."""
    if not time_limit_str:
        return None
    match = re.match(r'(\d+)\s*(second|seconds|minute|minutes|hour|hours)', time_limit_str, re.IGNORECASE)
    if not match:
        return None
    value, unit = match.groups()
    value = int(value)
    if unit.lower().startswith('second'):
        return value
    elif unit.lower().startswith('minute'):
        return value * 60
    elif unit.lower().startswith('hour'):
        return value * 3600
    return None

def extract_sms_command(command):
    global last_forwarding, pending_contact_prompt
    with cache_lock:
        if pending_contact_prompt:
            number_match = re.match(r'my\s+(\w+)\s+number\s+is\s+(\+\d{10,15})', command, re.IGNORECASE)
            if number_match:
                contact, phone_number = number_match.groups()
                add_contact(contact, phone_number)
                print(f"✅ Sweet! I’ve saved {contact} as {phone_number} for you.")
                original_command = pending_contact_prompt["command"]
                pending_contact_prompt = None
                return extract_sms_command(original_command)
    contact_pattern = r'(?:from|to|send it to|forward to)\s+(?:(\w+)|(\+\d{10,15}))'
    matches = re.findall(contact_pattern, command, re.IGNORECASE)
    contacts_to_check = [match[0] or match[1] for match in matches]
    for contact in contacts_to_check:
        if not is_valid_number(contact) and not resolve_contact(contact):
            print(f"🤔 Hmm, I don’t know who '{contact}' is yet. Could you tell me their number? Just say 'my {contact} number is +[phone_number]'.")
            with cache_lock:
                pending_contact_prompt = {"contact": contact, "command": command}
            return None
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract SMS assistant instructions from the user command. Return one JSON object per line with double-quoted keys and values.\n"
                        "Valid actions:\n"
                        "- \"send_sms\": Send a message (e.g., \"send an sms to +1234567890 saying hi\").\n"
                        "- \"add_contact\": Add a contact (e.g., \"add mom +1234567890\").\n"
                        "- \"start_forwarding\": Forward messages (e.g., \"forward from +1234567890 to mom\").\n"
                        "- \"stop_forwarding\": Stop forwarding (e.g., \"stop forwarding from mom to dad\").\n"
                        "- \"add_rule\": Add an auto-reply or forward rule (e.g., \"when +14372392448 sends hi reply hey\" or \"if I receive an sms from +14372392448 within 20 seconds say hey\").\n"
                        "- \"stop_rule\": Stop a specific or all rules (e.g., \"stop rule from navya\").\n"
                        "- \"list_rules\": List all active rules (e.g., \"list rules\").\n"
                        "- \"get_contact\": Get contact number (e.g., \"do I have mom saved in my contacts\").\n"
                        "- \"update_contact\": Update contact number (e.g., \"update mom +1234567890\").\n"
                        "- \"delete_contact\": Delete contact (e.g., \"delete mom\").\n"
                        "- \"list_contacts\": List all contacts (e.g., \"list contacts\").\n"
                        "- \"check_inbox\": Check inbox with a filter (e.g., \"show all messages\", \"show me all the messages that I have sent\", \"show me the first message I received from Navya\").\n"
                        "For \"send_sms\", include \"to_number\": \"+1234567890\" and \"body\": \"message\".\n"
                        "For \"add_rule\", include:\n"
                        "- \"from_number\": Phone number or null if contact-based.\n"
                        "- \"from_contact\": Contact name or null if number-based.\n"
                        "- \"action_type\": \"reply\" or \"forward\".\n"
                        "- \"reply_message\": Message to send (for reply) or null.\n"
                        "- \"forward_to_number\": Number to forward to or null.\n"
                        "- \"forward_to_contact\": Contact to forward to or null.\n"
                        "- \"condition\": Trigger phrase or null.\n"
                        "- \"timeout\": Delay in seconds (e.g., 20 for \"20 seconds\") or null.\n"
                        "- \"is_one_time\": true (default) or false.\n"
                        "For \"check_inbox\", identify the filter type:\n"
                        "- \"all\": All messages.\n"
                        "- \"sent\": Messages I have sent.\n"
                        "- \"received\": Messages I have received.\n"
                        "- \"first\": The earliest message.\n"
                        "- \"last\": The most recent message.\n"
                        "- \"business\", \"romantic\", etc.: Use AI to classify.\n"
                        "If a contact is specified, include \"contact\": \"contact_name\" or \"phone_number\": \"+1234567890\".\n"
                        "If the command includes \"today\", include \"date\": \"today\".\n"
                        "Examples:\n"
                        "- \"send an sms to +14372392448 saying hey\" -> {\"action\": \"send_sms\", \"to_number\": \"+14372392448\", \"body\": \"hey\"}\n"
                        "- \"if I receive an sms from +14372392448 within 20 seconds say hey\" -> {\"action\": \"add_rule\", \"from_number\": \"+14372392448\", \"action_type\": \"reply\", \"reply_message\": \"hey\", \"timeout\": 20, \"is_one_time\": true}\n"
                        "- \"show me all the messages that I have sent\" -> {\"action\": \"check_inbox\", \"filter\": \"sent\"}\n"
                        "- \"show me all the messages I have received from Navya\" -> {\"action\": \"check_inbox\", \"filter\": \"received\", \"contact\": \"Navya\"}\n"
                        "- \"show me the first message I sent\" -> {\"action\": \"check_inbox\", \"filter\": \"first\", \"direction\": \"sent\"}\n"
                        "- \"show me the last message I received from Navya\" -> {\"action\": \"check_inbox\", \"filter\": \"last\", \"direction\": \"received\", \"contact\": \"Navya\"}\n"
                        "- \"show all business messages\" -> {\"action\": \"check_inbox\", \"filter\": \"business\"}\n"
                        f"Recent forwarding context: from_number={last_forwarding.get('from_number')}, to_contact={last_forwarding.get('to_contact')}\n"
                        "Return {\"action\": \"other\"} if the command doesn't match any action."
                    )
                },
                {"role": "user", "content": command}
            ],
            temperature=0,
        )
        content = response.choices[0].message.content.strip().replace("'", '"')
        logger.info(f"Parsed command output: {content}")
        parsed_commands = []
        for line in content.splitlines():
            if line.strip():
                try:
                    cmd = json.loads(line)
                    if cmd.get("action") == "start_forwarding":
                        with cache_lock:
                            last_forwarding["from_number"] = cmd.get("from_number")
                            last_forwarding["to_contact"] = cmd.get("to_contact")
                            last_forwarding["to_number"] = cmd.get("to_number")
                    parsed_commands.append(cmd)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to parse JSON line: {line}, error: {e}")
                    continue
        return parsed_commands if parsed_commands else None
    except Exception as e:
        logger.error(f"Failed to extract command: {e}")
        return None

def classify_messages(messages, filter_type):
    classifications = []
    for message in messages:
        sender, text, timestamp, direction = message
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"Classify this SMS message as '{filter_type}' or 'other' based on its content. "
                            f"Return JSON: {{\"classification\": \"{filter_type}\"}} or {{\"classification\": \"other\"}}."
                        )
                    },
                    {"role": "user", "content": f"Message: {text}"}
                ],
                temperature=0,
            )
            content = response.choices[0].message.content.strip().replace("'", '"')
            result = json.loads(content)
            classifications.append(result.get("classification", "other"))
        except Exception as e:
            logger.error(f"Failed to classify message: {e}")
            classifications.append("other")
    return classifications

init_db()
threading.Thread(target=monitor_inbox, daemon=True).start()

if __name__ == "__main__":
    print("🤖 Hey there! I’m your SMS Assistant, ready to help. Type 'exit' to quit anytime.")
    while True:
        user_input = input("\nWhat’s on your mind? ").strip()
        if user_input.lower() in {"exit", "quit"}:
            print("👋 Catch you later! Bye!")
            break
        parsed_list = extract_sms_command(user_input)
        if not parsed_list:
            print("🤔 Oops, I didn’t quite get that. Could you try again?")
            continue
        for parsed in parsed_list:
            action = parsed.get("action")
            if action == "send_sms":
                to_number = parsed.get("to_number")
                contact = parsed.get("contact")
                body = parsed.get("body")
                logger.info(f"Processing send_sms: to_number={to_number}, contact={contact}, body={body}")
                if contact and not to_number:
                    to_number = resolve_contact(contact)
                    if not to_number:
                        print(f"🛑 Whoa, I don’t have {contact} in my contacts yet. Add them with 'add {contact} +[phone_number]'!")
                        continue
                if not is_valid_number(to_number):
                    print(f"🛑 Invalid number: {to_number or 'None'}. Please use a valid number starting with + followed by 10-15 digits.")
                    continue
                if not is_valid_body(body):
                    print(f"🛑 Invalid message: '{body or 'None'}'. Please provide a non-empty message.")
                    continue
                try:
                    resp = callnow_client.messages.create(
                        to=to_number,
                        from_=CALLNOWUSA_NUMBER,
                        body=body
                    )
                    print(f"✅ Message sent to {to_number}! They’ll love hearing from you.")
                except Exception as e:
                    logger.error(f"Failed to send SMS: {e}")
                    print("🛑 Failed to send message. Please try again later.")
            elif action == "add_contact":
                alias = parsed.get("alias")
                phone = parsed.get("phone_number")
                if alias and is_valid_number(phone):
                    add_contact(alias, phone)
                    print(f"✅ Added {alias} to your contacts with {phone}. All set!")
                else:
                    print("🛑 Something’s off with that name or number. Give me a valid one!")
            elif action == "update_contact":
                alias = parsed.get("alias")
                phone = parsed.get("phone_number")
                if alias and is_valid_number(phone):
                    add_contact(alias, phone)
                    print(f"✅ Updated {alias}’s number to {phone}. Good to go!")
                else:
                    print("🛑 Invalid name or number. Let’s fix that!")
            elif action == "delete_contact":
                alias = parsed.get("alias")
                if alias and resolve_contact(alias):
                    delete_contact(alias)
                    print(f"🗑️ {alias} is out of the contacts list now.")
                else:
                    print(f"🛑 I don’t have a {alias} to delete!")
            elif action == "get_contact":
                alias = parsed.get("alias")
                phone_number = parsed.get("phone_number")
                if phone_number:
                    with sqlite3.connect('contacts.db', check_same_thread=False) as conn:
                        c = conn.cursor()
                        c.execute('SELECT alias FROM contacts WHERE phone_number = ?', (phone_number,))
                        result = c.fetchone()
                    if result:
                        print(f"📇 Yep, {phone_number} is saved as {result[0]}.")
                    else:
                        print(f"🛑 Nope, {phone_number} isn’t in your contacts yet.")
                elif alias:
                    number = resolve_contact(alias)
                    if number:
                        print(f"📇 Found it! {alias} is {number}.")
                    else:
                        print(f"🛑 No {alias} in your contacts yet.")
                else:
                    print("🛑 Tell me a name or number to look up!")
            elif action == "list_contacts":
                all_contacts = list_contacts()
                if all_contacts:
                    print("📒 Here’s who you’ve got saved:")
                    for alias, phone in all_contacts:
                        print(f"- {alias} → {phone}")
                else:
                    print("🗒️ Your contact list is empty right now.")
            elif action == "start_forwarding":
                from_number = parsed.get("from_number")
                to_number = parsed.get("to_number")
                from_contact = parsed.get("from_contact")
                to_contact = parsed.get("to_contact")
                if from_contact:
                    from_number = resolve_contact(from_contact)
                    if not from_number:
                        print(f"🛑 I don’t know {from_contact} yet. Add them with 'add {from_contact} +[phone_number]'!")
                        continue
                if to_contact:
                    to_number = resolve_contact(to_contact)
                    if not to_number:
                        print(f"🛑 No number for {to_contact}. Add it with 'add {to_contact} +[phone_number]'!")
                        continue
                if is_valid_number(from_number) and is_valid_number(to_number):
                    try:
                        callnow_client.sms_forward(
                            to_number=from_number,
                            to_number2=to_number,
                            from_=CALLNOWUSA_NUMBER
                        )
                        print(f"✅ Now forwarding SMS from {from_number} to {to_number}. You’re all set!")
                    except Exception as e:
                        logger.error(f"Failed to start forwarding: {e}")
                        print("🛑 Failed to start forwarding. Please try again later.")
                else:
                    print("🛑 Those numbers don’t look right for forwarding. Check them!")
            elif action == "stop_forwarding":
                from_number = parsed.get("from_number") or last_forwarding.get("from_number")
                to_contact = parsed.get("to_contact") or last_forwarding.get("to_contact")
                to_number = parsed.get("to_number") or last_forwarding.get("to_number")
                if to_contact and not to_number:
                    to_number = resolve_contact(to_contact)
                if from_contact := parsed.get("from_contact"):
                    from_number = resolve_contact(from_contact)
                if is_valid_number(from_number) and is_valid_number(to_number):
                    try:
                        callnow_client.sms_forward_stop(
                            to_number=from_number,
                            to_number2=to_number,
                            from_=CALLNOWUSA_NUMBER
                        )
                        print(f"✅ Stopped forwarding from {from_number} to {to_number}. Done!")
                    except Exception as e:
                        logger.error(f"Failed to stop forwarding: {e}")
                        print("🛑 Failed to stop forwarding. Please try again later.")
                else:
                    print("🛑 Can’t stop forwarding—check those numbers!")
            elif action == "add_rule":
                from_number = parsed.get("from_number")
                from_contact = parsed.get("from_contact")
                action_type = parsed.get("action_type")
                reply_message = parsed.get("reply_message")
                forward_to_contact = parsed.get("forward_to_contact")
                forward_to_number = parsed.get("forward_to_number")
                condition = parsed.get("condition")
                start_time = parsed.get("start_time")
                end_time = parsed.get("end_time")
                timeout = parsed.get("timeout") or parse_time_limit(parsed.get("time_limit"))
                is_one_time = parsed.get("is_one_time", True)
                logger.info(f"Processing add_rule: from_number={from_number}, from_contact={from_contact}, action_type={action_type}, reply_message={reply_message}, timeout={timeout}")
                if from_contact:
                    from_number = resolve_contact(from_contact)
                    if not from_number:
                        print(f"🛑 I don’t know {from_contact} yet. Add them with 'add {from_contact} +[phone_number]'!")
                        continue
                if forward_to_contact:
                    forward_to_number = resolve_contact(forward_to_contact)
                    if not forward_to_number:
                        print(f"🛑 No number for {forward_to_contact}. Add it with 'add {forward_to_contact} +[phone_number]'!")
                        continue
                if is_valid_number(from_number) and (action_type == "reply" and is_valid_body(reply_message) or action_type == "forward" and is_valid_number(forward_to_number)):
                    if start_time and end_time and not is_valid_time_range(start_time, end_time):
                        print("🛑 Time range looks weird. Use 'HH:MM' like '09:00' to '17:00'!")
                        continue
                    rule_id = add_rule(from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time)
                    timeout_display = f"within {timeout} seconds" if timeout and timeout < 60 else f"within {timeout//60} minutes" if timeout else ""
                    print(f"✅ Got it! I’ll {action_type} {'to ' + (forward_to_contact or forward_to_number) if action_type == 'forward' else reply_message} for messages from {from_contact or from_number}" +
                          (f" when they say '{condition}'" if condition else "") +
                          (f" between {start_time} and {end_time}" if start_time and end_time else "") +
                          (f" {timeout_display}" if timeout_display else "") +
                          (" (just once)" if is_one_time else " (until you stop it)"))
                else:
                    print("🛑 Something’s wrong with that number or message. Let’s fix it!")
            elif action == "stop_rule":
                from_number = parsed.get("from_number")
                from_contact = parsed.get("from_contact")
                if from_contact:
                    from_number = resolve_contact(from_contact)
                if from_number:
                    delete_rule(from_number=from_number, to_contact=parsed.get("to_contact"))
                    print(f"✅ Stopped the rule for messages from {from_number}. All clear!")
                else:
                    delete_rule()
                    print("✅ Wiped out all rules for you! Anything else I can help with?")
            elif action == "list_rules":
                forward_only = "forward" in user_input.lower()
                rules = list_rules(forward_only)
                if rules:
                    if forward_only:
                        print("📬 Here are all your SMS forwarding rules:")
                    else:
                        print("📜 Here’s what I’m doing with your messages:")
                    for rule_id, from_contact, from_number, action_type, reply_message, forward_to_contact, forward_to_number, condition, start_time, end_time, timeout, is_one_time, created_at in rules:
                        action_desc = "Unknown" if not action_type else action_type.capitalize()
                        timeout_display = f"within {timeout} seconds" if timeout and timeout < 60 else f"within {timeout//60} minutes" if timeout else ""
                        print(f"- Rule {rule_id}: {action_desc} {'to ' + (forward_to_contact or forward_to_number) if action_type == 'forward' else reply_message} for messages from {from_contact or from_number}" +
                              (f" when they say '{condition}'" if condition else "") +
                              (f" between {start_time} and {end_time}" if start_time and end_time else "") +
                              (f" {timeout_display}" if timeout_display else "") +
                              (" (just once)" if is_one_time else " (until you stop it)") +
                              f" [Set up on: {created_at}]")
                else:
                    print("📭 Nothing’s set up right now!" if not forward_only else "📭 No forwarding rules active!")
            elif action == "check_inbox":
                filter_type = parsed.get("filter", "all")
                direction = parsed.get("direction")
                contact = parsed.get("contact")
                phone_number = parsed.get("phone_number")
                date_filter = parsed.get("date")
                if contact:
                    phone_number = resolve_contact(contact)
                    if not phone_number:
                        print(f"🛑 I don’t know {contact} yet. Add them with 'add {contact} +[phone_number]'!")
                        continue
                try:
                    inbox_message = callnow_client.check_inbox(from_=CALLNOWUSA_NUMBER)
                    response = inbox_message.fetch()
                    status_text = response.get("status", "")
                    all_messages = parse_inbox_messages(status_text)
                except Exception as e:
                    logger.error(f"Failed to check inbox: {e}")
                    print("🛑 Failed to check inbox. Please try again later.")
                    continue
                if not all_messages:
                    print("📭 Your inbox is quiet right now.")
                    continue
                if phone_number:
                    all_messages = [msg for msg in all_messages if (msg[0] == phone_number and msg[3] == "received") or (msg[0] == phone_number and msg[3] == "sent")]
                if direction:
                    all_messages = [msg for msg in all_messages if msg[3] == direction]
                if date_filter == "today":
                    today = datetime.now(pytz.timezone(TIMEZONE)).strftime("%Y-%m-%d")
                    all_messages = [msg for msg in all_messages if msg[2].startswith(today)]
                if filter_type in ["sent", "received"]:
                    filtered_messages = [msg for msg in all_messages if msg[3] == filter_type]
                elif filter_type in ["first", "last"]:
                    if not all_messages:
                        print("📭 No messages found.")
                        continue
                    sorted_messages = sorted(all_messages, key=lambda x: parse_datetime(x[2]))
                    filtered_messages = [sorted_messages[0]] if filter_type == "first" else [sorted_messages[-1]]
                elif filter_type == "all":
                    filtered_messages = all_messages
                else:
                    filtered_messages = [msg for msg, cls in zip(all_messages, classify_messages(all_messages, filter_type)) if cls == filter_type]
                if filtered_messages:
                    print(f"📨 You’ve got {len(filtered_messages)} messages to check out:")
                    for sender, text, timestamp, direction in filtered_messages:
                        print(f"{'Sent to' if direction == 'sent' else 'Received from'}: {sender}\nText: {text}\nTime: {timestamp}\n{'-' * 40}")
                else:
                    print("📭 No messages found matching your request.")
            else:
                print("🤔 Not sure what you mean. Want to try something like 'send an sms to +1234567890 saying hi'?")
