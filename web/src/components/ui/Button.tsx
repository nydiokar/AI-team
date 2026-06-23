/**
 * Button — shadcn-style (CVA + Radix Slot). Variants tuned to the cockpit
 * palette. The primary uses a cyan gradient like the mock's send button.
 */
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/cn";

const button = cva(
  "inline-flex items-center justify-center gap-2 rounded-full text-sm font-medium transition-colors outline-none focus-visible:ring-2 focus-visible:ring-accent/60 disabled:opacity-40 disabled:pointer-events-none",
  {
    variants: {
      variant: {
        primary:
          "bg-linear-to-b from-accent to-[#22b8c0] text-base font-semibold shadow-[0_8px_24px_-12px_var(--color-accent)] hover:brightness-110",
        ghost: "text-ink-soft hover:bg-surface-2 hover:text-ink",
        outline: "border border-hairline text-ink-soft hover:bg-surface-2 hover:text-ink",
        danger: "border border-bad/40 text-bad hover:bg-bad/10",
      },
      size: {
        sm: "h-9 px-3",
        md: "h-11 px-4",
        icon: "size-11",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof button> {
  asChild?: boolean;
}

export function Button({ className, variant, size, asChild, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return <Comp className={cn(button({ variant, size }), className)} {...props} />;
}
