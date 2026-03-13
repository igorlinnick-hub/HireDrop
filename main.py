import sys
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt

from database.db import init_db, save_job, get_all_jobs
from modules.scraper import scrape_jobs
from modules.filters import filter_jobs
from modules.telegram_bot import send_notification
from modules.email_parser import check_email_responses
from modules.ai_cover_letter import generate_cover_letter

console = Console()


def show_menu():
    console.print(Panel("[bold cyan]JobFlow Dashboard[/bold cyan]", expand=False))
    console.print("[1] Find Jobs")
    console.print("[2] View Saved Jobs")
    console.print("[3] Generate Cover Letter")
    console.print("[4] Check Email Responses")
    console.print("[5] Exit")
    return Prompt.ask("\nChoose an option", choices=["1", "2", "3", "4", "5"])


def find_jobs():
    console.print("\n[yellow]Scraping jobs from RemoteOK...[/yellow]")
    jobs = scrape_jobs()
    if not jobs:
        console.print("[red]No jobs found.[/red]")
        return

    console.print(f"[green]Found {len(jobs)} listings. Filtering...[/green]")
    filtered = filter_jobs(jobs)
    if not filtered:
        console.print("[red]No new jobs matching your keywords.[/red]")
        return

    for job in filtered:
        save_job(job["title"], job["company"], job["link"])

    console.print(f"[bold green]{len(filtered)} new jobs saved![/bold green]")
    send_notification(f"JobFlow: {len(filtered)} new jobs found!")


def view_saved_jobs():
    jobs = get_all_jobs()
    if not jobs:
        console.print("\n[red]No saved jobs yet.[/red]")
        return

    table = Table(title="Saved Jobs")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Title", style="white")
    table.add_column("Company", style="green")
    table.add_column("Status", style="yellow")
    table.add_column("Date", style="magenta")

    for job in jobs:
        table.add_row(
            str(job["id"]),
            job["title"],
            job["company"],
            job["status"],
            job["date_found"][:10],
        )

    console.print(table)


def generate_cover_letter_menu():
    view_saved_jobs()
    jobs = get_all_jobs()
    if not jobs:
        return

    job_id = Prompt.ask("\nEnter Job ID")
    job = None
    for j in jobs:
        if str(j["id"]) == job_id:
            job = j
            break

    if not job:
        console.print("[red]Job not found.[/red]")
        return

    letter = generate_cover_letter({"title": job["title"], "company": job["company"]})
    if letter:
        console.print(Panel(letter, title="Cover Letter", border_style="green"))
    else:
        console.print("[red]Failed to generate cover letter.[/red]")


def check_emails():
    console.print("\n[yellow]Checking email responses...[/yellow]")
    responses = check_email_responses()
    if not responses:
        console.print("[red]No matching email responses found.[/red]")
        return

    table = Table(title="Email Responses")
    table.add_column("Subject", style="white")
    table.add_column("From", style="green")
    table.add_column("Date", style="magenta")

    for r in responses:
        table.add_row(r["subject"], r["sender"], r["date"])

    console.print(table)


def main():
    init_db()
    console.print("[bold green]JobFlow initialized.[/bold green]\n")

    while True:
        choice = show_menu()
        if choice == "1":
            find_jobs()
        elif choice == "2":
            view_saved_jobs()
        elif choice == "3":
            generate_cover_letter_menu()
        elif choice == "4":
            check_emails()
        elif choice == "5":
            console.print("[bold cyan]Goodbye![/bold cyan]")
            sys.exit(0)
        console.print()


if __name__ == "__main__":
    main()
