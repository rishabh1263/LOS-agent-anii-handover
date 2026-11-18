import { createTheme, type MantineColorsTuple } from "@mantine/core";

// THE PALETTE -- "Midnight Sapphire" (owner 2026-10-29: one good colour, no theme switch): a midnight-navy rail, a
// sapphire-to-periwinkle accent, a warm amber highlight for what is current; red / yellow / green only for meaning.
const sapphire: MantineColorsTuple = ["#eef3ff", "#dbe4ff", "#b8c8ff", "#90a8ff", "#6f8bff", "#5a78fb", "#4c6bf2",
                                      "#3b57d6", "#314bbf", "#253da3"];

export const theme = createTheme({
  primaryColor: "sapphire",
  colors: { sapphire },
  primaryShade: 7,
  fontFamily: "Manrope, Inter, Segoe UI, system-ui, sans-serif",
  fontFamilyMonospace: "JetBrains Mono, Consolas, monospace",
  headings: { fontFamily: "Manrope, Inter, Segoe UI, system-ui, sans-serif", fontWeight: "800" },
  defaultRadius: "md",
  defaultGradient: { from: "sapphire.7", to: "indigo.4", deg: 120 },
  components: {
    Button: { defaultProps: { radius: "md" } },
    Paper: { defaultProps: { radius: "lg" } },
    Badge: { defaultProps: { radius: "sm", variant: "light" } },
    TextInput: { defaultProps: { radius: "md", size: "sm" } },
    // every dropdown: a soft card popping in under the field, roomy options, a check on the chosen one
    Select: {
      defaultProps: {
        radius: "md", size: "sm", checkIconPosition: "right", maxDropdownHeight: 280, withScrollArea: true,
        comboboxProps: { shadow: "lg", radius: "lg", offset: 6, transitionProps: { transition: "pop-top-left", duration: 160 } },
      },
      // theme classNames merge with a field's own (defaultProps classNames would be replaced by them)
      classNames: { dropdown: "dd-card", option: "dd-option", section: "dd-chevron", empty: "dd-empty" },
    },
    Textarea: { defaultProps: { radius: "md", autosize: true, minRows: 2 } },
  },
});
