const statusStyles: Record<string, string> = {
  draft:
    'text-text-muted border-border bg-bg-tertiary',
  validated:
    'text-neon-blue border-neon-blue bg-[rgba(0,187,255,0.1)] shadow-[0_0_8px_rgba(0,187,255,0.15)]',
  validating:
    'text-neon-blue border-neon-blue bg-[rgba(0,187,255,0.1)] animate-[pulse_1.5s_infinite]',
  deploying:
    'text-neon-yellow border-neon-yellow bg-[rgba(255,221,0,0.1)] animate-[pulse_1.5s_infinite]',
  live:
    'text-neon-green border-neon-green bg-[rgba(0,255,136,0.1)] shadow-[0_0_8px_rgba(0,255,136,0.15)]',
  error:
    'text-neon-red border-neon-red bg-[rgba(255,51,85,0.1)] shadow-[0_0_8px_rgba(255,51,85,0.15)]',
};

export default function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`font-mono text-[9px] py-0.5 px-1.5 border uppercase tracking-[1px] ${statusStyles[status] ?? statusStyles.draft}`}
    >
      {status}
    </span>
  );
}
