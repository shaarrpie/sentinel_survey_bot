(option_index) => {
    const result = { success: false, method: "none", error: "" };

    const optionSelectors = [
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
        ".form-check-label",
        ".custom-control-label",
        ".list-group-item",
        ".option"
    ];

    let optionElements = [];
    for (const sel of optionSelectors) {
        optionElements = Array.from(document.querySelectorAll(sel));
        if (optionElements.length > 0) break;
    }

    if (optionElements.length === 0) {
        const labels = document.querySelectorAll("label");
        if (labels.length > 1) {
            optionElements = Array.from(labels);
        }
    }

    if (option_index < 0 || option_index >= optionElements.length) {
        result.error = `Option index ${option_index} out of range (found ${optionElements.length} options)`;
        return result;
    }

    const option = optionElements[option_index];

    const clickTarget = option.tagName === "LABEL"
        ? (option.querySelector("input") || option)
        : option;

    clickTarget.scrollIntoView({ block: "center", behavior: "smooth" });

    try {
        clickTarget.click();
        result.method = "click";
    } catch (e) {
        try {
            const event = new MouseEvent("click", {
                bubbles: true,
                cancelable: true,
                view: window
            });
            clickTarget.dispatchEvent(event);
            result.method = "dispatchEvent";
        } catch (e2) {
            result.error = `Click failed: ${e.message}`;
            return result;
        }
    }

    const nativeAPIs = () => {
        if (typeof sp !== 'undefined' && sp.saveAnswer) {
            sp.saveAnswer();
            return "sp.saveAnswer";
        }
        if (typeof survey !== 'undefined' && survey.saveAnswer) {
            survey.saveAnswer();
            return "survey.saveAnswer";
        }
        if (typeof app !== 'undefined' && app.saveResponse) {
            app.saveResponse();
            return "app.saveResponse";
        }
        if (typeof window.saveAnswer === 'function') {
            window.saveAnswer();
            return "window.saveAnswer";
        }
        if (typeof window.submitAnswer === 'function') {
            window.submitAnswer();
            return "window.submitAnswer";
        }
        if (typeof window.next === 'function') {
            window.next();
            return "window.next";
        }
        if (typeof window.nextPage === 'function') {
            window.nextPage();
            return "window.nextPage";
        }
        if (typeof window.advance === 'function') {
            window.advance();
            return "window.advance";
        }
        const frameworkData = option.__vue__ || option.__reactFiber || option.__reactInternalInstance;
        if (frameworkData) {
            return "framework_detected";
        }
        return null;
    };

    const apiUsed = nativeAPIs();
    if (apiUsed) {
        result.method = apiUsed;
    }

    result.success = true;
    return result;
}
