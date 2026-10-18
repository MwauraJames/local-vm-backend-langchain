import os

from deepagents import create_deep_agent

from models import model
from backend import VirtualBoxSandbox

print("Initializing VirtualBox Sandbox...")
backend = VirtualBoxSandbox(
    vm_name=os.environ["VM_NAME"],
    host=os.getenv("VM_HOST", "127.0.0.1"),
    port=int(os.getenv("VM_PORT", "2222")),
    username=os.environ["VM_USERNAME"],
    password=os.getenv("VM_PASSWORD"),
)

# No custom tools any more. Because the VM is passed as `backend=`, Deep Agents' own
# built-in tools (execute, read_file, write_file, edit_file, ls, glob, grep) run inside it.
SYSTEM_PROMPT = (
    "You are an autonomous AI coding assistant with access to a Linux sandbox (a VM).\n"
    f"Your workspace is {backend.workspace_dir}. Shell commands start in that directory; "
    "use absolute paths under it with the file tools.\n"
    "STRICT CONTEXT AND EXECUTION RULES:\n"
    "1. Avoid verbose logging.\n"
    "2. When installing packages, always use quiet mode: `pip install -q <package> --break-system-packages`.\n"
    "3. Write scripts to files first, execute them, and present final metrics.\n"
    "4. Do not re-run directory inspections (ls, pwd) if the file was written successfully."
)

agent = create_deep_agent(
    model=model,
    backend=backend,
    system_prompt=SYSTEM_PROMPT,
)

try:
    print("Sending task to agent...\n")
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Write a python script to calculate the first 20 fibonacci numbers. Run the script and give me the results of the executed script \n"

                }
            ]
        }
    )
    print(result["messages"][-1].content)
finally:
    print("\nCleaning up sandbox...")
    backend.close()  # disconnects SSH and powers the VM off