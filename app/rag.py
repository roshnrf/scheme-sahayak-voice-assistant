"""RAG engine: retrieval (Phase 2's FAISS indices) + grounded generation with a confidence
threshold. Below the threshold, returns a fixed "not certain" message and skips the LLM
call entirely - never a confident guess, and saves Sarvam credits on top of it.

Standalone here (Phase 3); Phase 4 imports this same module into the Pipecat voice pipeline.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
from loguru import logger
from sarvamai import SarvamAI

load_dotenv()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EMBEDDING_MODEL = "BAAI/bge-m3"
LLM_MODEL = "sarvam-30b"
TOP_K = 3
CONFIDENCE_THRESHOLD = float(os.environ.get("RAG_CONFIDENCE_THRESHOLD", "0.38"))

# Fixed "not certain" fallback - never LLM-generated, so it can never hallucinate.
# Real Sarvam Mayura output (model="mayura:v1", mode="formal"), from the English source:
# "I am sorry, I could not find accurate enough information about this. Please check
# myscheme.gov.in directly, or try rephrasing your question."
# Flagged for your review, same pattern as the scraped dataset's MT fields.
NOT_CERTAIN_MESSAGE = {
    "hi": "क्षमा कीजिए, मुझे इस विषय में पर्याप्त सटीक जानकारी प्राप्त नहीं हुई। कृपया myscheme.gov.in पर सीधे जाँच करें, या अपने प्रश्न को पुनः व्यक्त करने का प्रयास करें।",
    "ml": "ക്ഷമിക്കണം, ഇതിനെക്കുറിച്ച് കൃത്യമായ വിവരങ്ങൾ കണ്ടെത്താൻ എനിക്ക് കഴിഞ്ഞില്ല. ദയവായി myscheme.gov.in നേരിട്ട് പരിശോധിക്കുക, അല്ലെങ്കിൽ നിങ്ങളുടെ ചോദ്യം പുനർനിർവചിക്കാൻ ശ്രമിക്കുക.",
}

# Real Sarvam Mayura output (model="mayura:v1", mode="formal"), from the English source:
# "You are a government scheme assistant. Answer only based on the information given below -
# do not add anything beyond this information. Always answer in {Hindi/Malayalam}, briefly
# and directly, since this will be spoken aloud. Do not use bullet points, numbering, bold
# text, or any formatting - write only plain, speakable sentences."
# (Explicitly no markdown/formatting: a first grounded-answer test came back with
# **bold**/bullet-list formatting that would read as garbled symbols through TTS.)
# Flagged for your review, same pattern as the scraped dataset's MT fields.
SYSTEM_PROMPT = {
    "hi": "आप एक सरकारी योजना सहायक हैं। केवल नीचे दी गई जानकारी के आधार पर ही उत्तर दीजिए - इस जानकारी से परे कुछ भी न जोड़ें। हमेशा हिंदी में, संक्षिप्त और सीधे, जवाब दें, क्योंकि यह ज़ोर से बोला जाएगा। बुलेट पॉइंट, संख्यांकन, बोल्ड टेक्स्ट या किसी भी प्रारूपण का उपयोग न करें - केवल सादे, बोलने योग्य वाक्यों को लिखें।",
    "ml": "താങ്കൾ ഒരു സർക്കാർ പദ്ധതി സഹായിയാണ്. ചുവടെ നൽകിയിരിക്കുന്ന വിവരങ്ങൾ മാത്രം അടിസ്ഥാനമാക്കി ഉത്തരം നൽകുക - ഈ വിവരങ്ങൾക്ക് പുറമേ മറ്റൊന്നും ചേർക്കരുത്. എല്ലായ്‌പ്പോഴും മലയാളത്തിൽ, ഹ്രസ്വമായും നേരിട്ട്, ഉത്തരം നൽകുക, കാരണം ഇത് ഉച്ചത്തിൽ സംസാരിക്കപ്പെടും. ബുള്ളറ്റ് പോയിന്‍റുകൾ, നമ്പറിംഗ്, ബോൾഡ് ടെക്സ്റ്റ് അല്ലെങ്കിൽ ഏതെങ്കിലും ഫോർമാറ്റിംഗ് എന്നിവ ഉപയോഗിക്കരുത് - പ്ലെയിൻ, സംസാരിക്കാവുന്ന വാക്യങ്ങൾ മാത്രം എഴുതുക.",
}

_embeddings = None
_stores: dict[str, FAISS] = {}
_client = None


def _get_embeddings() -> HuggingFaceEmbeddings:
    global _embeddings
    if _embeddings is None:
        _embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
    return _embeddings


def _get_store(lang: str) -> FAISS:
    if lang not in _stores:
        index_path = DATA_DIR / f"faiss_index_{lang}"
        _stores[lang] = FAISS.load_local(
            str(index_path), _get_embeddings(), allow_dangerous_deserialization=True
        )
    return _stores[lang]


def _get_client() -> SarvamAI:
    global _client
    if _client is None:
        api_key = os.environ.get("SARVAM_API_KEY")
        if not api_key:
            raise RuntimeError("SARVAM_API_KEY not set")
        _client = SarvamAI(api_subscription_key=api_key)
    return _client


def retrieve(query: str, lang: str, k: int = TOP_K) -> list[tuple[dict, float]]:
    """Returns [(scheme_record, confidence_score), ...] sorted best-first."""
    store = _get_store(lang)
    results = store.similarity_search_with_relevance_scores(query, k=k)
    return [(doc.metadata, float(score)) for doc, score in results]


@dataclass
class AnswerResult:
    answer: str
    grounded: bool
    confidence: float
    source_slug: str | None


def answer(query: str, lang: str) -> AnswerResult:
    results = retrieve(query, lang)
    if not results:
        return AnswerResult(NOT_CERTAIN_MESSAGE[lang], False, 0.0, None)

    top_record, top_score = results[0]
    if top_score < CONFIDENCE_THRESHOLD:
        return AnswerResult(NOT_CERTAIN_MESSAGE[lang], False, top_score, None)

    field = top_record[lang]
    context = "\n".join(
        part
        for part in [
            field.get("name", ""),
            field.get("benefits", ""),
            field.get("eligibility", ""),
            field.get("application_process", ""),
        ]
        if part
    )

    client = _get_client()
    response = client.chat.completions(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT[lang]},
            {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
        ],
    )
    text = response.choices[0].message.content
    if not text:
        # Sarvam occasionally returns an empty/None completion (observed in real voice
        # testing) - fall back to the not-certain message rather than pushing None text
        # downstream, which crashed the TTS frame validation.
        logger.warning(f"Empty LLM response for grounded query (source={top_record['slug']})")
        return AnswerResult(NOT_CERTAIN_MESSAGE[lang], False, top_score, None)

    return AnswerResult(text, True, top_score, top_record["slug"])
