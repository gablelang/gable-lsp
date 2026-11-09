-- Gable language server for Neovim 0.11+ (built-in vim.lsp.config).
--
-- Install gable-lsp so it is on PATH (`pipx install .`), or set `cmd` to an
-- absolute path. The server needs a `gable` compiler binary; pass its location
-- explicitly if it is not on PATH:
--
--   settings = { gable = { path = "/home/me/gable/build/gable" } }

vim.filetype.add({ extension = { gable = "gable" } })

vim.lsp.config("gable_lsp", {
  cmd = { "gable-lsp", "--stdio" },
  filetypes = { "gable" },
  root_markers = { ".git" },
  single_file_support = true,
  settings = {
    gable = {
      path = "gable",
      ["check.debounceMs"] = 150,
      ["check.timeoutMs"] = 10000,
      ["format.timeoutMs"] = 5000,
    },
  },
})

vim.lsp.enable("gable_lsp")

-- Optional: format on save. `gable-lsp` returns edits only; the editor applies
-- them, so nothing on disk changes behind your back.
-- vim.api.nvim_create_autocmd("BufWritePre", {
--   pattern = "*.gable",
--   callback = function() vim.lsp.buf.format({ async = false }) end,
-- })
