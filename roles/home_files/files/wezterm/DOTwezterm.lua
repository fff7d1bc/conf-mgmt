local wezterm = require 'wezterm';

wezterm.on("format-tab-title", function(tab, tabs, panes, config, hover, max_width)
  local title = tab.active_pane.title
  local max_length = 32

  if #title > max_length then
    local prefix_length = math.floor((max_length - 3) / 2) -- Length of the prefix part
    local suffix_length = max_length - prefix_length - 3 -- Length of the suffix part

    local prefix = title:sub(1, prefix_length)
    local suffix = title:sub(#title - suffix_length + 1, #title)

    title = prefix .. "..." .. suffix
  end

  local tab_index = tab.tab_index + 1  -- tab_index is zero-based, so add 1
  local formatted_title = string.format("%d:%s", tab_index, title)

  return {
    {Text = formatted_title},
  }
end)

return {
  check_for_updates = false,
  font = wezterm.font("JetBrains Mono"),
  font_size = 16.0,
  initial_cols = 90,
  initial_rows = 20,
  hide_tab_bar_if_only_one_tab = true,

  harfbuzz_features = {"calt=0", "clig=0", "liga=0"},

  color_scheme = 'Abernathy',
  colors = {
    background = '#090A0B'
  },

  tab_bar_at_bottom = true,
  -- use_fancy_tab_bar = false,
  -- We will limit mas width in format-tab-title override.
  tab_max_width = 128,


}
