import { Button } from "./ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Textarea } from "./ui/textarea";
import { Loader2 } from "lucide-react";

type ReasonTagOption = { value: string; label: string };

type Props = {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  action: "publish" | "delete";
  articleId: number | null;
  text: string;
  onTextChange: (value: string) => void;
  tags: string[];
  options: ReasonTagOption[];
  onToggleTag: (tag: string) => void;
  customTag: string;
  onCustomTagChange: (value: string) => void;
  onAddCustomTag: () => void;
  onSubmit: () => void;
  loading: boolean;
  loadingDelete?: boolean;
};

export function ReasonActionDialog({
  open,
  onOpenChange,
  action,
  articleId,
  text,
  onTextChange,
  tags,
  options,
  onToggleTag,
  customTag,
  onCustomTagChange,
  onAddCustomTag,
  onSubmit,
  loading,
  loadingDelete = false,
}: Props) {
  const isPublish = action === "publish";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl max-h-[92dvh] overflow-hidden p-0">
        <div className="flex h-full max-h-[92dvh] flex-col overflow-hidden">
          <DialogHeader className="px-6 pt-6 pb-2 shrink-0">
            <DialogTitle>{isPublish ? "Причина публикации" : "Причина удаления"}</DialogTitle>
            <DialogDescription>Добавь комментарий по статье #{articleId ?? "—"}.</DialogDescription>
          </DialogHeader>

          <div className="flex-1 overflow-y-auto px-6 pb-4">
            <div className="space-y-2">
              <Label>{isPublish ? "Почему публикуем?" : "Почему удаляем?"}</Label>
              <div className="space-y-2 rounded-md border border-border p-3">
                <div className="text-xs font-medium text-muted-foreground">
                  {isPublish ? "Теги причины публикации" : "Теги причины удаления"}
                </div>
                <div className="flex flex-wrap gap-2">
                  {options.map((item) => {
                    const active = tags.includes(item.value);
                    return (
                      <button
                        key={item.value}
                        type="button"
                        onClick={() => onToggleTag(item.value)}
                        className={`max-w-full rounded-full border px-2.5 py-1 text-xs transition-colors ${
                          active
                            ? isPublish
                              ? "border-primary/40 bg-primary/15 text-primary"
                              : "border-red-500/40 bg-red-500/15 text-red-200"
                            : "border-border bg-muted/20 text-muted-foreground hover:bg-muted/40"
                        }`}
                      >
                        {item.label}
                      </button>
                    );
                  })}
                </div>
                <div className="flex flex-wrap gap-2">
                  <Input
                    value={customTag}
                    onChange={(e) => onCustomTagChange(e.target.value)}
                    placeholder="Новый тег (например: local_policy_noise)"
                    className="h-8 min-w-[220px] flex-1"
                  />
                  <Button type="button" variant="outline" size="sm" onClick={onAddCustomTag}>
                    Добавить тег
                  </Button>
                </div>
              </div>
              <Textarea
                value={text}
                onChange={(e) => onTextChange(e.target.value)}
                rows={6}
                placeholder="Комментарий для истории действий и обучения"
              />
            </div>
          </div>

          <div className="shrink-0 border-t px-6 py-4">
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                Отмена
              </Button>
              <Button
                onClick={onSubmit}
                disabled={text.trim().length < 5 || loading}
                className={!isPublish ? "bg-destructive text-destructive-foreground hover:bg-destructive/90" : ""}
              >
                {!isPublish && loadingDelete ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                {isPublish ? "Опубликовать" : loadingDelete ? "Удаляем..." : "Удалить"}
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
