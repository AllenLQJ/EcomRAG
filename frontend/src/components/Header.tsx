interface Props { name: string; supportUrl?: string; sample: boolean; onReset: () => void }

export function Header({ name, supportUrl, sample, onReset }: Props) {
  return <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-line bg-card px-4 sm:h-16 sm:px-10">
    <div className="flex min-w-0 items-center gap-2 sm:gap-3">
      <span aria-hidden="true" className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-ink font-serif text-base text-card">栖</span>
      <div className="flex min-w-0 items-baseline gap-2 sm:gap-3">
        <span className="truncate font-serif text-base font-semibold sm:text-lg">{name}<span className="sm:hidden">客服</span></span>
        <span className="hidden text-sm text-muted sm:inline">客服中心</span>
      </div>
    </div>
    <div className="flex shrink-0 items-center gap-1 sm:gap-3">
      {sample && <span className="hidden rounded-full border border-line px-2.5 py-1 text-xs text-muted sm:inline">样例数据</span>}
      <button type="button" onClick={onReset} className="min-h-11 rounded-full px-2 text-xs text-muted hover:text-ink sm:px-3 sm:text-sm">新对话</button>
      {supportUrl && <a href={supportUrl} target="_blank" rel="noopener noreferrer" aria-label="联系人工客服" className="inline-flex min-h-11 items-center rounded-full px-2 text-xs sm:border sm:border-outline sm:px-4 sm:text-sm">
        <span className="sm:hidden">人工客服</span><span className="hidden sm:inline">联系人工客服</span>
      </a>}
    </div>
  </header>;
}
