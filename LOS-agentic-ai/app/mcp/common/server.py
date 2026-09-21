from mcp.server.fastmcp import FastMCP

mcp = FastMCP("los-common")


@mcp.tool()
def service_info() -> dict:
    return {
        "service": "los-common-mcp",
        "status": "available",
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
