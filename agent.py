import os
from uuid import uuid4
from deepagents import create_deep_agent
from langchain_core.tools import tool
from langchain_core.messages import ToolMessage, AnyMessage, SystemMessage

from models import model
from backend import VirtualBoxSandbox

def prune_tool_messages(messages: list[AnyMessage], keep_last_n_tools: int = 2) -> list[AnyMessage]:
    """
    Hollows out older ToolMessages to prevent token overflow while maintaining 
    the strict ToolCall -> ToolMessage pairing required by LLM APIs.
    """
    # 1. Count how many tool messages exist in the current state
    total_tools = sum(1 for msg in messages if isinstance(msg, ToolMessage))
    tools_processed = 0
    
    pruned_messages = []
    
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tools_processed += 1
            
            # 2. If it's an older tool message, replace its payload
            if tools_processed <= total_tools - keep_last_n_tools:
                # Create a lightweight stub that preserves the critical ID
                stub_content = f"[Context Pruned: Previous tool output omitted. Original length: {len(str(msg.content))} chars]"
                
                stub = ToolMessage(
                    content=stub_content,
                    tool_call_id=msg.tool_call_id,
                    name=msg.name
                )
                pruned_messages.append(stub)
            else:
                # Keep recent tool messages intact
                pruned_messages.append(msg)
        else:
            # Keep AI, Human, and System messages intact
            pruned_messages.append(msg)
            
    return pruned_messages

print("Initializing VirtualBox Sandbox...")
backend = VirtualBoxSandbox(
    vm_name=os.getenv("VM_NAME"),
    host=os.getenv("HOST"),
    port=int(os.getenv("PORT")),
    username=os.getenv("USERNAME"),
    password=os.getenv("PASSWORD")
)

@tool
def execute_shell(command: str) -> str:
    """
    Executes a shell command in the Linux sandbox. 
    Use this to run scripts, install packages, or explore the file system.
    """
    res = backend.execute_command(command)
    output = res['stdout'] + ("\n" + res['stderr'] if res['stderr'] else "")
    return f"Exit Code: {res['exit_code']}\nOutput:\n{output}"

@tool
def write_file(filename: str, content: str) -> str:
    """Writes code or data to a file in the sandbox."""
    return backend.write_file(filename, content)

@tool
def read_file(filename: str) -> str:
    """Reads the contents of a file in the sandbox."""
    return backend.read_file(filename)

@tool
def list_files(path: str = ".") -> str:
    """
    Lists the contents of a directory in the VM sandbox.
    Returns file permissions, ownership, size, and modification dates.
    """
    res = backend.list_directory(path)
    if res["exit_code"] == 0:
        return res["stdout"]
    return f"Error Listing Files:\n{res['stderr']}"

@tool
def grep_files(pattern: str, target_path: str = ".") -> str:
    """
    Searches for a specific text pattern or regex inside files in the sandbox.
    Returns the file names and exact line numbers where the pattern was found.
    """
    res = backend.grep_search(pattern, target_path)
    if res["exit_code"] == 0:
        return res["stdout"] if res["stdout"] else f"No matches found for '{pattern}'."
    if res["exit_code"] == 1 and not res["stderr"]:
        return f"No matches found for '{pattern}'."
    return f"Grep Error:\n{res['stderr']}"

@tool
def delete_path(path: str) -> str:
    """
    Permanently deletes a file or directory inside the sandbox workspace.
    """
    res = backend.delete_item(path)
    if res["exit_code"] == 0:
        return f"Successfully deleted: {path}"
    return f"Failed to delete {path}:\n{res['stderr']}"


# Group all tools for the agent
tools = [execute_shell, write_file, read_file, list_files, grep_files, delete_path]

# Define the system prompt cleanly as a constant
SYSTEM_PROMPT = (
    "You are an autonomous AI coding assistant with access to a Linux Sandbox.\n"
    "STRICT CONTEXT AND EXECUTION RULES:\n"
    "1. Avoid verbose logging: When writing ML training scripts (Keras/TF/PyTorch), "
    "always set verbosity to quiet or per-epoch only (e.g., `model.fit(..., verbose=2)` "
    "in Keras, NEVER verbose=1 which spams progress bars).\n"
    "2. When installing packages, always use quiet mode: `pip install -q <package> --break-system-packages`.\n"
    "3. Write scripts to files first, execute them, and present final metrics.\n"
    "4. Do not re-run directory inspections (ls, pwd) if the file was written successfully.\n"
    "5. You are in charge of the Linux VM sandbox. You can write files, execute shell commands, and read outputs."
)

def dynamic_state_modifier(state: dict) -> list[AnyMessage]:
    """
    Prunes old tool messages to save tokens and prepends the agent's system prompt.
    """
    # state["messages"] contains the raw history
    messages = state.get("messages", [])
    pruned = prune_tool_messages(messages, keep_last_n_tools=2)
    
    system_message = SystemMessage(content=SYSTEM_PROMPT)
    return [system_message] + pruned

# Instantiate the agent using the state modifier instead of system_prompt
agent = create_deep_agent(
    model=model,
    tools=tools,
    state_modifier=dynamic_state_modifier
)

try:
    print("Sending task to agent...\n")
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Build a local Retrieval-Augmented Generation (RAG) backend. "
                        "1. Create a directory called 'knowledge_base' and write 5 distinct text files into it, each containing a 2-paragraph technical summary of a different programming language (Python, Rust, Go, C++, JavaScript). "
                        "2. Write a script (`build_index.py`) that uses `sentence-transformers` (use a small model like 'all-MiniLM-L6-v2') to embed these texts and store them in a local `faiss-cpu` vector index. "
                        "3. Write a second script (`query.py`) that loads the FAISS index, accepts a query string, finds the top 2 most semantically similar documents, and prints their filenames and similarity scores. "
                        "4. Run the pipeline and query it with: 'Which language enforces memory safety without a garbage collector?' "
                        "Show me the stdout of the query script."
                    )
                }
            ]
        }
    )
    print(result["messages"][-1].content)
finally:
    # Safely teardown the VM sandbox
    print("\nCleaning up sandbox...")
    backend.cleanup()