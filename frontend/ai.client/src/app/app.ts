import { Component, signal } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { Sidenav } from './components/sidenav/sidenav';
import { Topnav } from './components/topnav/topnav';
import { ErrorToastComponent } from './components/error-toast/error-toast.component';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, Sidenav, Topnav, ErrorToastComponent],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly title = signal('boisestate.ai');
}
