from typing import List, Optional, Dict
from queue import Queue
import hashlib

from pynvim import Nvim
from pynvim.api import Buffer
from molten.code_cell import CodeCell

from molten.options import MoltenOptions
from molten.images import Canvas
from molten.position import Position
from molten.utils import notify_info
from molten.outputbuffer import OutputBuffer
from molten.outputchunks import OutputStatus
from molten.runtime import JupyterRuntime


# Handles a Single Kernel that can be attached to multiple buffers
# Other MoltenKernels can be attached to the same buffers
class MoltenKernel:
    nvim: Nvim
    canvas: Canvas
    highlight_namespace: int
    extmark_namespace: int
    buffers: List[Buffer]

    runtime: JupyterRuntime

    # name unique to this specific jupyter runtime. Only used within Molten. Human Readable
    kernel_id: str

    outputs: Dict[CodeCell, OutputBuffer]
    current_output: Optional[CodeCell]
    queued_outputs: "Queue[CodeCell]"

    selected_cell: Optional[CodeCell]
    should_show_floating_win: bool
    updating_interface: bool

    options: MoltenOptions

    def __init__(
        self,
        nvim: Nvim,
        canvas: Canvas,
        highlight_namespace: int,
        extmark_namespace: int,
        main_buffer: Buffer,
        options: MoltenOptions,
        kernel_name: str,
        kernel_id: str,
    ):
        self.nvim = nvim
        self.canvas = canvas
        self.highlight_namespace = highlight_namespace
        self.extmark_namespace = extmark_namespace
        self.buffers = [main_buffer]

        self._doautocmd("MoltenInitPre")

        self.runtime = JupyterRuntime(nvim, kernel_name, options)
        self.kernel_id = kernel_id

        self.outputs = {}
        self.current_output = None
        self.queued_outputs = Queue()

        self.selected_cell = None
        self.should_show_floating_win = False
        self.updating_interface = False

        self.options = options

    def _doautocmd(self, autocmd: str) -> None:
        assert " " not in autocmd
        self.nvim.command(f"doautocmd User {autocmd}")

    def add_nvim_buffer(self, buffer: Buffer) -> None:
        self.buffers.append(buffer)

    def deinit(self) -> None:
        self._doautocmd("MoltenDeinitPre")
        self.runtime.deinit()
        self._doautocmd("MoltenDeinitPost")

    def interrupt(self) -> None:
        self.runtime.interrupt()

    def restart(self, delete_outputs: bool = False) -> None:
        if delete_outputs:
            self.outputs = {}
            self.clear_interface()
            self.clear_open_output_windows()
            self.clear_virt_outputs()

        self.runtime.restart()

    def run_code(self, code: str, span: CodeCell) -> None:
        self.delete_overlapping_cells(span)
        self.runtime.run_code(code)

        self.outputs[span] = OutputBuffer(
            self.nvim, self.canvas, self.extmark_namespace, self.options
        )
        self.queued_outputs.put(span)

        self.selected_cell = span

        if not self.options.virt_text_output:
            self.should_show_floating_win = True

        self.update_interface()

        self._check_if_done_running()

    def reevaluate_cell(self) -> bool:
        self.selected_cell = self._get_selected_span()
        if self.selected_cell is None:
            return False

        code = self.selected_cell.get_text(self.nvim)

        self.run_code(code, self.selected_cell)
        return True

    def _check_if_done_running(self) -> None:
        # TODO: refactor
        is_idle = (self.current_output is None or not self.current_output in self.outputs) or (
            self.current_output is not None
            and self.outputs[self.current_output].output.status == OutputStatus.DONE
        )
        if is_idle and not self.queued_outputs.empty():
            key = self.queued_outputs.get_nowait()
            self.current_output = key

    def tick(self) -> None:
        self._check_if_done_running()

        was_ready = self.runtime.is_ready()
        if self.current_output is None or not self.current_output in self.outputs:
            did_stuff = self.runtime.tick(None)
        else:
            did_stuff = self.runtime.tick(self.outputs[self.current_output].output)
        if did_stuff:
            self.update_interface()
        if not was_ready and self.runtime.is_ready():
            notify_info(self.nvim, f"Kernel '{self.runtime.kernel_name}' is ready.")

    def enter_output(self) -> None:
        if self.selected_cell is not None:
            if self.options.enter_output_behavior != "no_open":
                self.should_show_floating_win = True
            self.should_show_floating_win = self.outputs[self.selected_cell].enter(
                self.selected_cell.end
            )

    def _get_cursor_position(self) -> Position:
        _, lineno, colno, _, _ = self.nvim.funcs.getcurpos()
        return Position(self.nvim.current.buffer.number, lineno - 1, colno - 1)

    def clear_interface(self) -> None:
        if self.updating_interface:
            return

        for buffer in self.buffers:
            self.nvim.funcs.nvim_buf_clear_namespace(
                buffer.number,
                self.highlight_namespace,
                0,
                -1,
            )

    def clear_open_output_windows(self) -> None:
        for output in self.outputs.values():
            output.clear_float_win()

    def clear_virt_outputs(self) -> None:
        for cell, output in self.outputs.items():
            output.clear_virt_output(cell.bufno)

    def _get_selected_span(self) -> Optional[CodeCell]:
        current_position = self._get_cursor_position()
        selected = None
        for span in reversed(self.outputs.keys()):
            if current_position in span:
                selected = span
                break

        return selected

    def delete_overlapping_cells(self, span: CodeCell) -> None:
        """Delete the code cells in this kernel that overlap with the given span"""
        for output_span in list(self.outputs.keys()):
            if output_span.overlaps(span):
                if self.current_output == output_span:
                    self.current_output = None
                self.outputs[output_span].clear_float_win()
                self.outputs[output_span].clear_virt_output(span.bufno)
                del self.outputs[output_span]
                output_span.clear_interface(self.highlight_namespace)

    def delete_cell(self) -> None:
        self.selected_cell = self._get_selected_span()
        if self.selected_cell is None:
            return

        self.outputs[self.selected_cell].clear_float_win()
        self.outputs[self.selected_cell].clear_virt_output(self.selected_cell.bufno)
        self.selected_cell.clear_interface(self.highlight_namespace)
        del self.outputs[self.selected_cell]
        self.selected_cell = None

    def update_interface(self) -> None:
        buffer_numbers = [buf.number for buf in self.buffers]
        if self.nvim.current.buffer.number not in buffer_numbers:
            return

        if self.nvim.current.window.buffer.number not in buffer_numbers:
            return

        self.updating_interface = True
        new_selected_cell = self._get_selected_span()

        # Clear the cell we just left
        if self.selected_cell != new_selected_cell and self.selected_cell is not None:
            if self.selected_cell in self.outputs:
                self.outputs[self.selected_cell].clear_float_win()
            self.selected_cell.clear_interface(self.highlight_namespace)

        if new_selected_cell is None:
            self.should_show_floating_win = False

        self.selected_cell = new_selected_cell

        if self.selected_cell is not None:
            self._show_selected(self.selected_cell)

        if self.options.virt_text_output:
            for span, output in self.outputs.items():
                output.show_virtual_output(span.end)

        self.canvas.present()

        self.updating_interface = False

    def on_cursor_moved(self, scrolled=False) -> None:
        new_selected_cell = self._get_selected_span()

        if (
            self.selected_cell is None
            and new_selected_cell is not None
            and self.options.auto_open_output
        ):
            self.should_show_floating_win = True

        if self.selected_cell == new_selected_cell and new_selected_cell is not None:
            if (
                scrolled
                and new_selected_cell.end.lineno < self.nvim.funcs.line("w$")
                and self.should_show_floating_win
            ):
                self.update_interface()
            return

        self.update_interface()

    def _show_selected(self, span: CodeCell) -> None:
        """Show the selected cell. Can only have a selected cell in the current buffer"""
        buf = self.nvim.current.buffer
        if buf.number not in [b.number for b in self.buffers]:
            return

        if span.begin.lineno == span.end.lineno:
            self.nvim.funcs.nvim_buf_add_highlight(
                buf.number,
                self.highlight_namespace,
                self.options.hl.cell,
                span.begin.lineno,
                span.begin.colno,
                span.end.colno,
            )
        else:
            self.nvim.funcs.nvim_buf_add_highlight(
                buf.number,
                self.highlight_namespace,
                self.options.hl.cell,
                span.begin.lineno,
                span.begin.colno,
                -1,
            )
            for lineno in range(span.begin.lineno + 1, span.end.lineno):
                self.nvim.funcs.nvim_buf_add_highlight(
                    buf.number,
                    self.highlight_namespace,
                    self.options.hl.cell,
                    lineno,
                    0,
                    -1,
                )
            self.nvim.funcs.nvim_buf_add_highlight(
                buf.number,
                self.highlight_namespace,
                self.options.hl.cell,
                span.end.lineno,
                0,
                span.end.colno,
            )

        if self.should_show_floating_win:
            self.outputs[span].show_floating_win(span.end)
        else:
            self.outputs[span].clear_float_win()

    def _get_content_checksum(self) -> str:
        return hashlib.md5(
            "\n".join(self.nvim.current.buffer.api.get_lines(0, -1, True)).encode("utf-8")
        ).hexdigest()
