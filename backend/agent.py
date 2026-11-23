"""LangGraph graph (from langGraphimp.py). The approval step takes a list of ticked item ids."""
import json
import os
from typing import Annotated, TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

SYSTEM_PROMPT = """You help a university student find opportunities and upcoming deadlines.

1. Call search_opportunities ONCE with the student's request. Use the CV file path, interests and
   eligibility info from the user's message exactly as given, and pass the "Types of opportunity wanted"
   as the types list (for example ["spring week", "internship"]). The tool searches widely and already
   narrows the results down to the best matches, one company programme each, so do not call it again.
2. Then reply with a short, plain-text summary of what you found.
Do NOT try to schedule anything: the student chooses what to apply to, and the calendar plan is made afterwards.
If a tool returns an error, say so plainly instead of inventing results."""


class State(TypedDict):
    messages: Annotated[list, add_messages]
    scheduled_events: list
    summary: str
    booking_report: list


def content_texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    texts = []
    for block in content or []:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            texts.append(block["text"])
    return texts


def extract_items(content) -> list[dict]:
    items = []
    for text in content_texts(content):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            data = data["result"]
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            items.extend(d for d in data if isinstance(d, dict))
    return items


def build_graph(tools, plan_tool):
    model = init_chat_model(os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5"), model_provider="anthropic", max_tokens=4096)
    model_with_tools = model.bind_tools(tools)

    async def researcher_node(state: State):
        resp = await model_with_tools.ainvoke([SystemMessage(content=SYSTEM_PROMPT)] + state["messages"])
        return {"messages": [resp]}

    def route_after_researcher(state: State):
        messages = state["messages"]
        if getattr(messages[-1], "tool_calls", None):
            return "tools"
        if any(isinstance(m, ToolMessage) for m in messages):
            return "calendar_schedule"
        return END

    async def calendar_schedule_node(state: State):
        summary = "".join(content_texts(state["messages"][-1].content)).strip()  # Claude replies in content blocks

        found = {}
        for msg in state["messages"]:
            if isinstance(msg, ToolMessage) and msg.name == "search_opportunities":
                for it in extract_items(msg.content):
                    if it.get("title") and not it.get("error") and (it.get("deadline") or it.get("watch")):
                        found[(it["title"], it.get("url"))] = it
        items = list(found.values())

        def finish(note, report=None, scheduled=None):
            return {
                "summary": summary,
                "booking_report": report or [note],
                "scheduled_events": scheduled or [],
                "messages": [AIMessage(content=f"{summary}\n\n{note}")],
            }

        if not items:
            return finish("Nothing to schedule.")

        # The UI shows these as a checklist and resumes with the list of ticked ids.
        selected = interrupt(
            {
                "items": [
                    {
                        "id": n,
                        "title": it["title"],
                        "deadline": it.get("deadline"),
                        "type": it.get("type"),
                        "source": it.get("source", "web"),
                        "url": it.get("url") or it.get("link"),
                        "raw": it,
                    }
                    for n, it in enumerate(items)
                ]
            }
        )
        chosen = [items[n] for n in (selected or []) if isinstance(n, int) and 0 <= n < len(items)]
        if not chosen:
            return finish("Nothing was added to your calendar.")

        result = await plan_tool.ainvoke({"items": chosen})
        report = [f"{b.get('title')}: {b.get('status')}" for b in extract_items(result)]
        return finish("Calendar:\n" + "\n".join(report), report, chosen)

    g = StateGraph(State)
    g.add_node("Researcher", researcher_node)
    g.add_node("tools", ToolNode(tools))
    g.add_node("calendar_schedule", calendar_schedule_node)

    g.add_edge(START, "Researcher")
    g.add_conditional_edges(
        "Researcher",
        route_after_researcher,
        {"tools": "tools", "calendar_schedule": "calendar_schedule", END: END},
    )
    g.add_edge("tools", "Researcher")
    g.add_edge("calendar_schedule", END)

    return g.compile(checkpointer=MemorySaver())
