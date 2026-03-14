from agents.mcp.server import MCPServerStdio
from agentd.patch import patch_openai_with_mcp
from openai import OpenAI


def test_mcp_chat_completions():
    """MCP-patched client with filesystem server via Chat Completions API."""
    fs_server = MCPServerStdio(
        params={
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/"],
        },
        cache_tools_list=True
    )

    client = patch_openai_with_mcp(OpenAI())

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "user", "content": "List the files in /tmp/ using the tool"}
        ],
        mcp_servers=[fs_server],
        mcp_strict=True
    )

    print(response.choices[0].message.content)


def test_embeddings():
    """Patched client embeddings passthrough."""
    client = patch_openai_with_mcp(OpenAI())

    response = client.embeddings.create(
        model="gemini/gemini-embedding-001",
        input="Hello world",
    )

    print(response.data[0].embedding)


if __name__ == "__main__":
    test_mcp_chat_completions()
    test_embeddings()
