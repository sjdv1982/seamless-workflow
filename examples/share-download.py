"""Serve a zip download of Context cells, built when the URL is requested."""

from seamless import Cell
from seamless_workflow import Context, shareserver


context = Context()
context.title = Cell(celltype="text")
context.title.set("daily report")
context.metadata = Cell(celltype="plain")
context.metadata.set({"record_count": 3, "complete": True})
context.compute()

# Members are the cells' buffers; a dict names the members in the archive.
url = context.shares.add_download(
    {"title.txt": "title", "metadata.json": "metadata"},
    path="report.zip",
    format="zip",
)
print(f"Download the archive from {url}")
input("Press Enter to stop serving the archive... ")
context.close()
shareserver.close()
