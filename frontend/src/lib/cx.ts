/** Join class names, dropping anything falsy. The one string helper we need. */
export function cx(...parts: (string | number | false | null | undefined)[]): string {
  return parts.filter((part) => typeof part === 'string' && part.length > 0).join(' ')
}
