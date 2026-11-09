-- Neovim 0.9/0.10 with nvim-lspconfig: drop this in
-- ~/.config/nvim/after/ftplugin/gable.lua, or use vim.lsp.config as in
-- gable.lua.

local lspconfig = require("lspconfig")
local configs = require("lspconfig.configs")

if not configs.gable_lsp then
  configs.gable_lsp = {
    default_config = {
      cmd = { "gable-lsp", "--stdio" },
      filetypes = { "gable" },
      root_dir = lspconfig.util.root_pattern(".git", "."),
      single_file_support = true,
      settings = {
        gable = { path = "gable" },
      },
    },
  }
end

lspconfig.gable_lsp.setup({})
