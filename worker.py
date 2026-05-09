import asyncio
import concurrent.futures
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def preflight_checks():
    """Validate environment before starting the worker."""
    errors = []

    if not os.environ.get("GROQ_API_KEY"):
        errors.append(
            "GROQ_API_KEY not set. "
            "Copy .env.example to .env and paste your key."
        )

    # Check Kubernetes connectivity (import triggers config load)
    try:
        from activities.k8s_activities import v1
        v1.list_namespace(limit=1)
    except RuntimeError as e:
        errors.append(str(e))
    except Exception as e:
        errors.append(f"Kubernetes cluster unreachable: {e}")

    if errors:
        print("\n  Preflight checks failed:\n")
        for err in errors:
            print(f"    [FAIL] {err}")
        print()
        sys.exit(1)

    print("  [OK] )Groq API key")
    print("  [OK] Kubernetes cluster")


preflight_checks()

from temporalio.client import Client
from temporalio.worker import Worker

from activities.k8s_activities import scan_cluster, get_pod_details, execute_fix
from activities.llm_activities import diagnose_pod
from activities.chat_activities import (
    call_claude,
    list_pods_activity,
    get_pod_details_activity,
    get_pod_logs_activity,
    get_pod_events_activity,
)
from workflows.healer_workflow import HealerWorkflow
from workflows.conversation_workflow import ConversationWorkflow


async def main():
    client = await Client.connect("localhost:7233")

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        worker = Worker(
            client,
            task_queue="kubehealer",
            workflows=[HealerWorkflow, ConversationWorkflow],
            activities=[
                # Healing activities
                scan_cluster,
                get_pod_details,
                execute_fix,
                diagnose_pod,
                # Conversation activities
                call_claude,
                list_pods_activity,
                get_pod_details_activity,
                get_pod_logs_activity,
                get_pod_events_activity,
            ],
            activity_executor=executor,
        )

        print("\n  KubeHealer worker started. Waiting for tasks...\n")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
