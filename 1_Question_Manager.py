import streamlit as st
import json
from pathlib import Path
from datetime import datetime

st.set_page_config(
    page_title="Question Manager",
    page_icon="📝",
    layout="wide",
)

st.title("📝 Question Manager")
st.caption(
    "Add, edit, remove and organize your own chatbot test questions. "
    "Custom questions are shared with the main API Tester page during this session."
)

# Initialize shared state
if "custom_questions" not in st.session_state:
    st.session_state["custom_questions"] = []

questions = st.session_state["custom_questions"]

st.subheader("Add Questions")

input_mode = st.radio(
    "Input mode",
    ["Single Question", "Multiple Questions"],
    horizontal=True,
)

if input_mode == "Single Question":
    question = st.text_input(
        "Question",
        placeholder="Example: What is the applicant's current application status?",
    )

    if st.button("➕ Add Question", type="primary"):
        question = question.strip()

        if not question:
            st.warning("Please enter a question.")
        elif question in questions:
            st.warning("This question already exists.")
        else:
            questions.append(question)
            st.session_state["custom_questions"] = questions
            st.success("Question added.")
            st.rerun()

else:
    questions_text = st.text_area(
        "Enter multiple questions, one per line",
        height=220,
        placeholder=(
            "What is the applicant's name?\n"
            "What documents are missing?\n"
            "Why is this case in review?"
        ),
    )

    if st.button("➕ Add All Questions", type="primary"):
        incoming = [
            q.strip()
            for q in questions_text.splitlines()
            if q.strip()
        ]

        added = 0
        duplicates = 0

        for q in incoming:
            if q in questions:
                duplicates += 1
            else:
                questions.append(q)
                added += 1

        st.session_state["custom_questions"] = questions

        if added:
            st.success(
                f"Added {added} question(s). "
                f"{duplicates} duplicate(s) skipped."
            )
        else:
            st.warning("No new questions were added.")

        st.rerun()


st.divider()

# Search / list
st.subheader("My Custom Questions")

search = st.text_input(
    "Search questions",
    placeholder="Search by keyword...",
)

filtered = [
    (index, question)
    for index, question in enumerate(questions)
    if not search.strip()
    or search.lower().strip() in question.lower()
]

c1, c2, c3 = st.columns(3)
c1.metric("Total Custom Questions", len(questions))
c2.metric("Matching Questions", len(filtered))
c3.metric("Built-in Questions", 107)

if not filtered:
    st.info("No custom questions found.")

for index, question in filtered:
    row1, row2 = st.columns([0.88, 0.12])

    with row1:
        edited = st.text_input(
            f"Q{index + 1}",
            value=question,
            key=f"question_edit_{index}",
            label_visibility="collapsed",
        )

    with row2:
        if st.button("🗑️", key=f"delete_{index}", help="Delete question"):
            questions.pop(index)
            st.session_state["custom_questions"] = questions
            st.rerun()

    if edited.strip() != question:
        questions[index] = edited.strip()
        st.session_state["custom_questions"] = questions

# Save state
st.session_state["custom_questions"] = [
    q.strip()
    for q in st.session_state["custom_questions"]
    if q.strip()
]

st.divider()

# Import / export custom questions
st.subheader("Import / Export")

export_payload = {
    "exported_at": datetime.now().isoformat(),
    "questions": st.session_state["custom_questions"],
}

col1, col2 = st.columns(2)

with col1:
    st.download_button(
        "⬇️ Export JSON",
        data=json.dumps(export_payload, ensure_ascii=False, indent=2),
        file_name="custom_chatbot_questions.json",
        mime="application/json",
        use_container_width=True,
    )

with col2:
    uploaded = st.file_uploader(
        "Import JSON",
        type=["json"],
        label_visibility="collapsed",
    )

if uploaded is not None:
    try:
        imported = json.load(uploaded)

        if isinstance(imported, dict):
            imported_questions = imported.get("questions", [])
        elif isinstance(imported, list):
            imported_questions = imported
        else:
            imported_questions = []

        if not isinstance(imported_questions, list):
            raise ValueError("questions must be a list")

        added = 0
        for q in imported_questions:
            q = str(q).strip()
            if q and q not in questions:
                questions.append(q)
                added += 1

        st.session_state["custom_questions"] = questions
        st.success(f"Imported {added} new question(s).")
        st.rerun()

    except Exception as exc:
        st.error(f"Invalid question JSON: {exc}")

if st.button("🗑️ Clear All Custom Questions"):
    st.session_state["custom_questions"] = []
    st.success("All custom questions removed.")
    st.rerun()

st.info(
    "Note: questions are stored in Streamlit session state. "
    "They remain available while this browser session is active. "
    "Use Export JSON if you want to keep them permanently."
)
