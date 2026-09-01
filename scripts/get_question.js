() => {
    const question_data = {
        QUESTION: "",
        TYPE: "unknown",
        OPTIONS: [],
        RAW_TEXT: ""
    };

    const selectors = {
        question: [
            ".surveyQuestion .surveyQuestionText",
            ".surveyQuestionText",
            ".question-text",
            ".questionText",
            "[data-question-text]",
            ".qtext",
            ".question-title",
            ".survey-title",
            "h2.question",
            "h3.question",
            ".form-title",
            ".field-label",
            "label.question",
            "[role='heading']",
            ".ql-editor p",
            ".question-header"
        ],
        options: [
            ".surveyQuestionAnswer",
            ".answer-option",
            ".surveyAnswer",
            ".option-label",
            ".answerText",
            "[data-answer]",
            ".choice",
            ".response",
            "label.answer",
            ".radio-label",
            ".checkbox-label",
            ".select-option",
            ".dropdown-option",
            ".multi-select-option",
            ".single-select-option",
            "input[type='radio']",
            "input[type='checkbox']",
            "select option",
            ".form-check-label",
            ".custom-control-label",
            ".list-group-item",
            ".option"
        ],
        questionContainer: [
            ".surveyQuestion",
            ".question-container",
            ".survey-question",
            ".quiz-question",
            "[data-question]",
            ".form-group",
            ".field",
            ".question",
            ".survey-item",
            ".form-field"
        ]
    };

    let questionEl = null;
    for (const sel of selectors.question) {
        questionEl = document.querySelector(sel);
        if (questionEl) break;
    }

    if (questionEl) {
        question_data.QUESTION = questionEl.innerText.trim();
    } else {
        const container = document.querySelector(selectors.questionContainer.join(", "));
        if (container) {
            const headings = container.querySelectorAll("h1, h2, h3, h4, h5, h6, .title, .header");
            if (headings.length > 0) {
                question_data.QUESTION = headings[0].innerText.trim();
            } else {
                const firstText = container.innerText.split("\n").find(line => line.trim().length > 10);
                if (firstText) {
                    question_data.QUESTION = firstText.trim();
                }
            }
        }
    }

    if (!question_data.QUESTION) {
        const bodyText = document.body.innerText.trim();
        const lines = bodyText.split("\n").filter(l => l.trim().length > 5);
        if (lines.length > 0) {
            question_data.QUESTION = lines[0].trim();
        }
    }

    let optionElements = [];
    for (const sel of selectors.options) {
        optionElements = Array.from(document.querySelectorAll(sel));
        if (optionElements.length > 0) break;
    }

    if (optionElements.length === 0) {
        const container = document.querySelector(selectors.questionContainer.join(", ")) || document.body;
        const labels = container.querySelectorAll("label");
        if (labels.length > 1) {
            optionElements = Array.from(labels);
        }
    }

    const seenTexts = new Set();
    optionElements.forEach((opt, idx) => {
        let text = opt.innerText || opt.textContent || "";
        text = text.trim();
        if (opt.tagName === "INPUT") {
            const label = opt.closest("label") || document.querySelector(`label[for='${opt.id}']`);
            if (label) {
                text = label.innerText.trim() || text;
            }
            if (!text) text = opt.value || opt.getAttribute("aria-label") || "";
        }
        if (opt.tagName === "OPTION") {
            text = opt.text || opt.innerText || "";
        }
        text = text.trim();
        if (text && !seenTexts.has(text.toLowerCase())) {
            seenTexts.add(text.toLowerCase());
            question_data.OPTIONS.push({
                index: idx,
                text: text,
                value: opt.value || opt.getAttribute("value") || "",
                tag: opt.tagName.toLowerCase(),
                type: opt.type || opt.getAttribute("type") || ""
            });
        }
    });

    const hasRadio = optionElements.some(el =>
        el.type === "radio" || el.getAttribute("role") === "radio"
    );
    const hasCheckbox = optionElements.some(el =>
        el.type === "checkbox" || el.getAttribute("role") === "checkbox"
    );
    const hasSelect = optionElements.some(el =>
        el.tagName === "SELECT" || el.classList.contains("select") ||
        el.getAttribute("role") === "listbox" || el.getAttribute("role") === "combobox"
    );
    const hasTextbox = optionElements.some(el =>
        el.tagName === "TEXTAREA" ||
        (el.tagName === "INPUT" && ["text", "email", "number", "tel", "date", "url"].includes(el.type))
    );

    if (optionElements.length > 0 && optionElements[0].classList.contains("activeSelectMenu")) {
        question_data.TYPE = "CHECKBOX";
    } else if (hasCheckbox) {
        question_data.TYPE = "CHECKBOX";
    } else if (hasRadio) {
        question_data.TYPE = "SELECT";
    } else if (hasSelect) {
        question_data.TYPE = "DROPDOWN";
    } else if (hasTextbox) {
        question_data.TYPE = "TEXT";
    } else if (optionElements.length > 0) {
        question_data.TYPE = "SELECT";
    }

    question_data.RAW_TEXT = document.body.innerText.trim().substring(0, 2000);

    return question_data;
}
