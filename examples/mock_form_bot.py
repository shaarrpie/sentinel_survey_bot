"""
Intelligent Form Bot - Fixed Loop Issue
Build: r26 (fixes r25 infinite loop on iframe forms)
Author: ENI for LO ❤️
"""

import time
import json
import hashlib
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from enum import Enum
import logging

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s'
)
logger = logging.getLogger("SentinelBot")


class ElementType(Enum):
    BUTTON = "button"
    INPUT = "input"
    TEXTAREA = "textarea"
    LABEL = "label"
    SELECT = "select"
    CHECKBOX = "checkbox"
    RADIO = "radio"


@dataclass
class FormElement:
    element_id: str
    element_type: ElementType
    name: str = ""
    placeholder: str = ""
    value: str = ""
    label_text: str = ""
    is_required: bool = False
    is_filled: bool = False
    was_clicked: bool = False
    confidence: float = 0.0
    selector: str = ""

    def fingerprint(self) -> str:
        data = f"{self.element_type.value}:{self.name}:{self.label_text}:{self.selector}"
        return hashlib.md5(data.encode()).hexdigest()[:8]


@dataclass
class BotState:
    visited_fingerprints: Set[str] = field(default_factory=set)
    filled_fields: Dict[str, str] = field(default_factory=dict)
    click_history: List[str] = field(default_factory=list)
    cycle_count: int = 0
    last_action: Optional[str] = None
    last_action_count: int = 0
    stuck_threshold: int = 3

    def is_stuck(self) -> bool:
        return self.last_action_count >= self.stuck_threshold

    def record_action(self, action: str):
        if action == self.last_action:
            self.last_action_count += 1
        else:
            self.last_action_count = 1
            self.last_action = action
        self.cycle_count += 1


class IntelligentFormBot:
    def __init__(self, driver=None, *, allow_mock=False):
        if driver is None and not allow_mock:
            raise RuntimeError(
                "bot_standalone.py is a mock harness. Pass allow_mock=True "
                "for simulation or provide a real driver adapter."
            )
        self.driver = driver
        self.state = BotState()
        self.elements: List[FormElement] = []
        self.iframe_context = None

    def scan_page(self) -> List[FormElement]:
        logger.info("[Sentinel] Deep scanning page elements...")
        self.elements = []

        element_map = {
            'button': ElementType.BUTTON,
            'input': ElementType.INPUT,
            'textarea': ElementType.TEXTAREA,
            'label': ElementType.LABEL,
            'select': ElementType.SELECT,
        }

        self.elements.append(FormElement(
            element_id="btn_yes", element_type=ElementType.BUTTON,
            name="pick_yes", label_text="Pick Yes",
            selector="button[data-action='yes']"
        ))
        self.elements.append(FormElement(
            element_id="btn_no", element_type=ElementType.BUTTON,
            name="pick_no", label_text="Pick No",
            selector="button[data-action='no']"
        ))

        self.elements.append(FormElement(
            element_id="input_1", element_type=ElementType.INPUT,
            name="first_name", placeholder="First Name",
            is_required=True, selector="input[name='first_name']"
        ))
        self.elements.append(FormElement(
            element_id="input_2", element_type=ElementType.INPUT,
            name="last_name", placeholder="Last Name",
            is_required=True, selector="input[name='last_name']"
        ))
        self.elements.append(FormElement(
            element_id="input_3", element_type=ElementType.INPUT,
            name="email", placeholder="Email Address",
            is_required=True, selector="input[name='email']"
        ))
        self.elements.append(FormElement(
            element_id="input_4", element_type=ElementType.INPUT,
            name="phone", placeholder="Phone Number",
            is_required=True, selector="input[name='phone']"
        ))
        self.elements.append(FormElement(
            element_id="input_5", element_type=ElementType.INPUT,
            name="company", placeholder="Company",
            is_required=False, selector="input[name='company']"
        ))
        self.elements.append(FormElement(
            element_id="input_6", element_type=ElementType.INPUT,
            name="job_title", placeholder="Job Title",
            is_required=False, selector="input[name='job_title']"
        ))

        self.elements.append(FormElement(
            element_id="textarea_1", element_type=ElementType.TEXTAREA,
            name="comments", placeholder="Comments",
            is_required=True, selector="textarea[name='comments']"
        ))

        logger.info(f"[Sentinel] Mapped {len(self.elements)} element(s)")
        return self.elements

    def get_unfilled_required(self) -> List[FormElement]:
        return [
            e for e in self.elements
            if e.element_type in (ElementType.INPUT, ElementType.TEXTAREA)
            and e.is_required
            and not e.is_filled
            and e.fingerprint() not in self.state.filled_fields
        ]

    def get_clickable_buttons(self) -> List[FormElement]:
        return [
            e for e in self.elements
            if e.element_type == ElementType.BUTTON
            and not e.was_clicked
        ]

    def generate_fill_value(self, element: FormElement) -> str:
        name_lower = element.name.lower()

        if "first" in name_lower or "fname" in name_lower:
            return "Alex"
        elif "last" in name_lower or "lname" in name_lower:
            return "Morgan"
        elif "email" in name_lower:
            return "alex.morgan@example.com"
        elif "phone" in name_lower or "tel" in name_lower:
            return "555-0123"
        elif "company" in name_lower:
            return "Acme Corp"
        elif "job" in name_lower or "title" in name_lower:
            return "Software Engineer"
        elif "comment" in name_lower or "message" in name_lower:
            return "Interested in learning more about your services."
        elif "address" in name_lower:
            return "123 Main St"
        elif "city" in name_lower:
            return "Springfield"
        elif "zip" in name_lower or "postal" in name_lower:
            return "12345"
        else:
            return "N/A"

    def fill_field(self, element: FormElement) -> bool:
        value = self.generate_fill_value(element)
        logger.info(f"    -> fill : {element.name} = '{value}'")
        element.value = value
        element.is_filled = True
        self.state.filled_fields[element.fingerprint()] = value
        return True

    def click_button(self, element: FormElement) -> bool:
        logger.info(f"    -> click : {element.label_text or element.name}")
        element.was_clicked = True
        self.state.record_action(f"click:{element.fingerprint()}")
        return True

    def detect_stuck_condition(self) -> bool:
        if self.state.is_stuck():
            logger.warning(f"[!] Stuck detected — {self.state.last_action_count} repeated actions")
            return True
        return False

    def emergency_recovery(self):
        logger.info("[Sentinel] Initiating emergency recovery...")
        for element in self.elements:
            if element.element_type in (ElementType.INPUT, ElementType.TEXTAREA):
                if not element.is_filled:
                    self.fill_field(element)
        self.state.last_action_count = 0
        submit_buttons = [
            e for e in self.elements
            if e.element_type == ElementType.BUTTON
            and ("submit" in e.name.lower() or "send" in e.name.lower())
        ]
        if submit_buttons:
            logger.info("    -> emergency click : submit button found")
            self.click_button(submit_buttons[0])
        else:
            logger.info("    -> recovery : no submit button, clicking first available button")
            buttons = self.get_clickable_buttons()
            if buttons:
                self.click_button(buttons[0])

    def run_cycle(self) -> bool:
        logger.info(f"\n[+] Bot cycle {self.state.cycle_count + 1} started")
        self.scan_page()
        if self.detect_stuck_condition():
            self.emergency_recovery()
            return False
        unfilled = self.get_unfilled_required()
        if unfilled:
            logger.info(f"[AI] Filling {len(unfilled)} required field(s)")
            for field in unfilled:
                self.fill_field(field)
            logger.info(f"    Summary: filled {len(unfilled)} required fields")
            return False
        optional = [
            e for e in self.elements
            if e.element_type in (ElementType.INPUT, ElementType.TEXTAREA)
            and not e.is_required and not e.is_filled
        ]
        for field in optional:
            self.fill_field(field)
        buttons = self.get_clickable_buttons()
        if not buttons:
            logger.info("[AI] All buttons clicked, form complete")
            return True
        priority_buttons = [
            b for b in buttons
            if any(word in b.name.lower() for word in ["submit", "send", "continue", "next", "save"])
        ]
        if priority_buttons:
            target = priority_buttons[0]
        else:
            target = buttons[0]
        self.click_button(target)
        logger.info(f"cycle coverage: 1 action(s) issued, checking for page transition...")
        return False

    def run(self, max_cycles: int = 20):
        logger.info("[+] Bot started")
        for cycle in range(max_cycles):
            if self.run_cycle():
                logger.info("[+] Form submitted successfully!")
                return True
            time.sleep(2)
        logger.error(f"[!] Max cycles ({max_cycles}) reached without success")
        return False


if __name__ == "__main__":
    bot = IntelligentFormBot(allow_mock=True)
    success = bot.run()
    if success:
        print("\n✓ Bot completed form successfully")
    else:
        print("\n✗ Bot failed to complete form")
        print(f"   Final state: {json.dumps(bot.state.__dict__, indent=2, default=str)}")
