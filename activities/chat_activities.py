import os
import json
import uuid
from groq import Groq
from kubernetes import client, config
from temporalio import activity
from temporalio.exceptions import ApplicationError
from models import ClaudeRequest, ClaudeResponse


def _init_k8s():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        try:
            config.load_kube_config()
        except config.ConfigException:
            raise RuntimeError("No Kubernetes cluster found.")
    return client.CoreV1Api()


v1 = _init_k8s()


@activity.defn
async def call_claude(request: ClaudeRequest) -> ClaudeResponse:
    activity.logger.info(f"Calling Groq ({len(request.messages)} messages)")

    ai = Groq(api_key=os.environ.get("GROQ_API_KEY"))

    # Build groq tools
    groq_tools = []
    for tool in request.tools:
        schema = tool.get("input_schema", {})
        props = schema.get("properties", {})
        parameters = {
            "type": "object",
            "properties": {},
        }
        for k, v in props.items():
            parameters["properties"][k] = {
                "type": v.get("type", "string"),
                "description": v.get("description", ""),
            }
            if "default" in v:
                parameters["properties"][k]["default"] = v["default"]
        if schema.get("required"):
            parameters["required"] = schema["required"]
        groq_tools.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": parameters,
            }
        })

    # Build messages
    messages = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})

    for msg in request.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, list):
            # Check for tool results
            tool_results = [b for b in content if b.get("type") == "tool_result"]
            text_blocks = [b for b in content if b.get("type") == "text"]
            tool_use_blocks = [b for b in content if b.get("type") == "tool_use"]

            if tool_results:
                for tr in tool_results:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", str(uuid.uuid4())),
                        "content": str(tr.get("content", "")),
                    })
                continue

            if tool_use_blocks and role == "assistant":
                tool_calls = []
                text_content = " ".join(b["text"] for b in text_blocks) if text_blocks else None
                for tu in tool_use_blocks:
                    tool_calls.append({
                        "id": tu.get("id", str(uuid.uuid4())),
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu.get("input", {})),
                        }
                    })
                msg_dict = {"role": "assistant", "tool_calls": tool_calls}
                if text_content:
                    msg_dict["content"] = text_content
                messages.append(msg_dict)
                continue

            text_parts = [b["text"] for b in text_blocks]
            content = " ".join(text_parts) if text_parts else "(empty)"

        if role not in ("user", "assistant", "system", "tool"):
            role = "user"

        if content or role == "assistant":
            messages.append({"role": role, "content": content})

    try:
        kwargs = {
            "model": "llama-3.3-70b-versatile",
            "max_tokens": 4096,
            "messages": messages,
        }
        if groq_tools:
            kwargs["tools"] = groq_tools
            kwargs["tool_choice"] = "auto"

        response = ai.chat.completions.create(**kwargs)
    except Exception as e:
        raise ApplicationError(f"Groq API error: {e}", non_retryable=True)

    resp_msg = response.choices[0].message
    content_dicts = []
    stop_reason = "end_turn"

    if resp_msg.content:
        content_dicts.append({"type": "text", "text": resp_msg.content})

    if resp_msg.tool_calls:
        stop_reason = "tool_use"
        for tc in resp_msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except Exception:
                args = {}
            content_dicts.append({
                "type": "tool_use",
                "id": tc.id,
                "name": tc.function.name,
                "input": args,
            })

    if not content_dicts:
        content_dicts.append({"type": "text", "text": "(no response)"})

    return ClaudeResponse(stop_reason=stop_reason, content=content_dicts)


@activity.defn
async def list_pods_activity(namespace: str) -> str:
    activity.logger.info(f"Listing pods in namespace {namespace}")
    pods = v1.list_namespaced_pod(namespace=namespace)
    lines = [f"{'NAME':<50} {'STATUS':<25} {'READY':<8} {'RESTARTS'}"]
    lines.append("-" * 95)
    for pod in pods.items:
        name = pod.metadata.name
        phase = pod.status.phase or "Unknown"
        ready = "0/0"
        restarts = 0
        if pod.status.container_statuses:
            total = len(pod.status.container_statuses)
            ready_count = sum(1 for cs in pod.status.container_statuses if cs.ready)
            ready = f"{ready_count}/{total}"
            restarts = sum(cs.restart_count for cs in pod.status.container_statuses)
            for cs in pod.status.container_statuses:
                if cs.state and cs.state.waiting and cs.state.waiting.reason:
                    phase = cs.state.waiting.reason
                    break
                if cs.state and cs.state.terminated and cs.state.terminated.reason:
                    phase = cs.state.terminated.reason
                    break
        lines.append(f"{name:<50} {phase:<25} {ready:<8} {restarts}")
    return "\n".join(lines)


@activity.defn
async def get_pod_details_activity(pod_name: str, namespace: str) -> str:
    activity.logger.info(f"Getting details for pod {pod_name}")
    try:
        pod = v1.read_namespaced_pod(name=pod_name, namespace=namespace)
        lines = []
        lines.append(f"Pod: {pod.metadata.name}")
        lines.append(f"Namespace: {pod.metadata.namespace}")
        lines.append(f"Node: {pod.spec.node_name}")
        lines.append(f"Phase: {pod.status.phase}")
        if pod.status.container_statuses:
            for cs in pod.status.container_statuses:
                lines.append(f"Container: {cs.name}")
                lines.append(f"  Image: {cs.image}")
                lines.append(f"  Ready: {cs.ready}")
                lines.append(f"  Restarts: {cs.restart_count}")
                if cs.state.waiting:
                    lines.append(f"  Waiting: {cs.state.waiting.reason}")
                if cs.state.terminated:
                    lines.append(f"  Terminated: {cs.state.terminated.reason}")
        return "\n".join(lines)
    except Exception as e:
        return f"Could not get pod details: {e}"


@activity.defn
async def get_pod_logs_activity(pod_name: str, namespace: str, tail_lines: int) -> str:
    activity.logger.info(f"Getting logs for pod {pod_name}")
    try:
        logs = v1.read_namespaced_pod_log(
            name=pod_name, namespace=namespace, tail_lines=tail_lines
        )
        return logs if logs else "(no log output)"
    except Exception as e:
        return f"Could not get logs: {e}"


@activity.defn
async def get_pod_events_activity(pod_name: str, namespace: str) -> str:
    activity.logger.info(f"Getting events for pod {pod_name}")
    events = v1.list_namespaced_event(
        namespace=namespace,
        field_selector=f"involvedObject.name={pod_name},involvedObject.kind=Pod",
    )
    if not events.items:
        return f"No events found for pod {pod_name}."
    lines = []
    for event in events.items[-15:]:
        lines.append(f"[{event.type:<8}] {event.reason:<25} {event.message}")
    return "\n".join(lines)
