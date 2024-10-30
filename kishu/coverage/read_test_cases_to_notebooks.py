import nbformat
import random

if __name__ == '__main__':
    nb = nbformat.v4.new_notebook()

        # read notebook
    with open("tests/notebooks/QiskitDemo_NCSA_May2023.ipynb") as f:
        nb = nbformat.read(f, as_version=4)
    src_cells = []

    for cell in nb["cells"]:
        if cell["cell_type"] == "code":
            src_cells.append(cell)


    # Insert random cells
    jumbled_cells = []
    for i in range(1000 - len(src_cells)):
        cellnum = random.randint(1, len(src_cells) - 1)
        cell = src_cells[cellnum]
        # wrap in try except
        new_cell_code = "try:\n"
        for line in cell.source.split("\n"):
            new_cell_code += "    " + line + "\n"
        new_cell_code += "except:\n"
        new_cell_code += "    pass"
        new_cell = nbformat.v4.new_code_cell(source=new_cell_code)
        jumbled_cells.append(new_cell)

    nb["cells"] = src_cells + jumbled_cells

    with open("tests/notebooks/Qiskit_jumbled.ipynb", mode="w", encoding="utf-8") as f:
        nbformat.write(nb, f)
