import { Component, signal } from '@angular/core';
import { RouterOutlet } from '@angular/router';
import { Sidenav } from './components/sidenav/sidenav';
import { Topnav } from './components/topnav/topnav';
import { ModelSettings } from './components/model-settings/model-settings';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, Sidenav, Topnav, ModelSettings],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly title = signal('ai.client');
}
