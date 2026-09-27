-- Run a JavaScript file in a Safari tab and print what it returns.
-- Passing the script as a file avoids quoting a whole JS program through
-- AppleScript string escaping, which is unreadable and breaks on quotes.
-- Optional second argument: the id of the window to run in (default: front).
on run argv
	set jsFile to item 1 of argv
	set winId to ""
	if (count of argv) > 1 then set winId to item 2 of argv
	set js to (read POSIX file jsFile as «class utf8»)
	tell application "Safari"
		if winId is "" then
			set result_value to (do JavaScript js in current tab of front window)
		else
			set result_value to (do JavaScript js in current tab of window id (winId as integer))
		end if
	end tell
	if result_value is missing value then
		return "null"
	end if
	return result_value as text
end run
