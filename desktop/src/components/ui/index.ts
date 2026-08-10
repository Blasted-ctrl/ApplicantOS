/**
 * The primitive barrel (`docs/UI.md` §7).
 *
 * Every component the screens are allowed to build from, and which kind each one is:
 *
 * | Component | Kind | Source |
 * |---|---|---|
 * | Button | `shadcn` | Radix `Slot` + `cva` |
 * | Input · SearchInput · Field · Label | `shadcn` | native + `cva` |
 * | Select | `shadcn` | `@radix-ui/react-select` |
 * | Textarea · CharacterCounter | `shadcn` | native |
 * | Checkbox · Radio · Switch | `shadcn` | `@radix-ui/react-{checkbox,radio-group,switch}` |
 * | Badge · StatusBadge | `shadcn+` | custom over the tone table |
 * | StatusDot | `custom` | — |
 * | Card | `shadcn+` | custom |
 * | StatTile · Sparkline | `custom` | inline SVG |
 * | DataTable | `custom` | `@tanstack/react-virtual` |
 * | Tabs · SegmentedControl | `shadcn+` | `@radix-ui/react-tabs` + Motion `layoutId` |
 * | Dialog · ConfirmDialog | `shadcn` | `@radix-ui/react-dialog` + Motion |
 * | Sheet | `shadcn` | `@radix-ui/react-dialog` + Motion drag |
 * | Tooltip | `shadcn` | `@radix-ui/react-tooltip` |
 * | Toaster | `sonner` | — |
 * | Skeleton | `shadcn+` | custom |
 * | EmptyState · NoResults | `custom` | — |
 * | CommandPalette | `cmdk` | `cmdk` inside a Radix `Dialog` |
 * | ScoreBar | `custom` | — |
 * | Timeline | `custom` | Motion `V.fade` only |
 * | LogStream | `custom` | `@tanstack/react-virtual` |
 * | ProgressRing · IndeterminateBar | `custom` | inline SVG |
 * | Kbd · KbdCombo | `custom` | — |
 * | SidebarItem | `custom` | — |
 *
 * Import from `@/components/ui` rather than from the individual files: the surface is the
 * contract, and a screen reaching past it into a file's internals is how a primitive stops
 * being one.
 */

export { Badge, StatusBadge, type BadgeProps, type StatusBadgeProps } from './badge';
export { Button, type ButtonProps } from './button';
export {
  Card,
  CardBody,
  CardFooter,
  CardGroupLabel,
  CardHeader,
  type CardHeaderProps,
  type CardProps,
} from './card';
export {
  Checkbox,
  CheckboxField,
  Radio,
  RadioField,
  RadioGroup,
  Switch,
  type CheckboxFieldProps,
  type CheckboxProps,
  type RadioFieldProps,
  type SwitchProps,
} from './checkbox';
export {
  CommandGroup,
  CommandItem,
  CommandPalette,
  PALETTE_GROUPS,
  type CommandItemProps,
  type CommandPaletteProps,
  type PaletteGroup,
} from './command-palette';
export {
  ConfirmDialog,
  Dialog,
  DialogClose,
  DialogContent,
  DialogTrigger,
  type ConfirmDialogProps,
  type DialogContentProps,
} from './dialog';
export { EmptyState, NoResults, type EmptyStateProps, type NoResultsProps } from './empty-state';
export { FileDropzone, type FileDropzoneProps } from './file-drop';
export {
  Field,
  FieldError,
  HelpText,
  Input,
  Label,
  SearchInput,
  type FieldProps,
  type InputProps,
  type SearchInputProps,
} from './input';
export { Kbd, KbdCombo, type KbdComboProps, type KbdProps } from './kbd';
export { LogStream, type LogStreamProps } from './log-stream';
export {
  IndeterminateBar,
  ProgressRing,
  type IndeterminateBarProps,
  type ProgressRingProps,
} from './progress-ring';
export {
  ScoreBar,
  ScoreComponentRow,
  type ScoreBarProps,
  type ScoreComponentRowProps,
} from './score-bar';
export {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
  type SelectTriggerProps,
} from './select';
export { Sheet, SheetClose, SheetContent, SheetTrigger, type SheetContentProps } from './sheet';
export {
  SidebarGroupLabel,
  SidebarItem,
  type SidebarItemProps,
} from './sidebar-item';
export {
  SKELETON_PRESETS,
  Skeleton,
  SkeletonRows,
  type SkeletonProps,
  type SkeletonRowsProps,
} from './skeleton';
export {
  Sparkline,
  StatTile,
  StatTileSkeleton,
  type SparklineProps,
  type StatTileProps,
} from './stat-tile';
export { StatusDot, type StatusDotProps } from './status-dot';
export {
  DataTable,
  VIRTUALIZE_THRESHOLD,
  type DataTableColumn,
  type DataTableProps,
  type SortState,
} from './table';
export {
  SegmentedControl,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
  type SegmentOption,
  type SegmentedControlProps,
} from './tabs';
export {
  CharacterCounter,
  Textarea,
  type CharacterCounterProps,
  type TextareaProps,
} from './textarea';
export { Toaster, type ToasterProps } from './toast';
export {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipRoot,
  TooltipTrigger,
  type TooltipProps,
  type TooltipProviderProps,
} from './tooltip';
export { Timeline, type TimelineItem, type TimelineProps } from './timeline';
export { buttonVariants, fieldVariants } from './variants';
